# Architecture

RemCTL intentionally splits reads and writes.

## Components

```text
remctl (Python)
  ├─ reads Reminders CoreData SQLite database
  ├─ formats human and JSON output
  ├─ calls remctl-bridge for normal writes
  └─ calls remctl-private for opt-in private metadata writes

remctl-bridge (Swift)
  └─ writes through EventKit

remctl-private (Objective-C)
  └─ writes selected private metadata through private ReminderKit APIs

remctl-permissions (Swift/AppKit)
  └─ guides Full Disk Access setup with draggable targets
```

There is no daemon, localhost API, launch agent, or token setup in RemCTL 1.0. The CLI is the only runtime surface.

## Reads

Direct reads use the Reminders CoreData stores. Each account (iCloud, Exchange,
CalDAV, on-device local) has its own database file in the Stores directory:

```text
~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/Data-*.sqlite
```

See [Multi-Account Support](#multi-account-support) for how RemCTL selects among
and reads across these stores.

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

## Writes

Writes go through Apple-supported APIs:

1. `remctl-bridge` writes via EventKit. This is the normal path for create, edit, moving reminders between lists, complete, delete, recurrence, alarms, URLs appended to notes, and list management. The Python CLI validates user input first, and the bridge also rejects malformed due dates, recurrence rules, alarms, priorities, and location payloads if called directly.
2. AppleScript is a fallback for operations that still need Reminders.app automation behavior.

There is also an explicitly unsupported opt-in helper:

3. `remctl-private` writes selected private metadata through Apple's private ReminderKit framework. It is gated by `--private`, never writes SQLite directly, and is intentionally excluded from normal write behavior. Verified private writes include web rich URL attachments, hashtag labels, section assignment/creation, shared-list assignment, rich subtasks, image attachments, real flag state, urgent state, Early Reminders, location alarms, list appearance metadata such as exact colors, private emblem names, and emoji badges, list and smart-list pin state, Groceries list metadata and categorization verification, experimental custom smart-list creation/editing/deletion for verified materializing Reminders filters, and Reminders template create/apply/delete. Rich URL and image edit writes are additive; RemCTL does not remove or replace existing rich links/images. For rich subtasks, `remctl-private` creates the child and applies private child metadata, then `remctl-bridge` applies public child fields such as notes and due dates. Generic file/PDF attachments are rejected because Reminders does not reliably show them even when private rows sync.

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
- `ZSHOULDCATEGORIZEGROCERYITEMS`, `ZSHOULDAUTOCATEGORIZEITEMS`, `ZSHOULDSUGGESTCONVERSIONTOGROCERYLIST`, and `ZGROCERYLOCALEID` describe Reminders' special Groceries lists.

RemCTL reads these fields directly for display and verification. `list-symbols` exposes the 71 official bundled emblem names discovered from RemindersUICore's `ListBadge*` assets. Its terminal glyphs are approximate Unicode fallbacks; `list-symbols --preview` and `list-symbols --html PATH` load the native badge assets from RemindersUICore and write a standalone HTML contact sheet with interactive official color swatches. Normal `list-create --color NAME` still writes through EventKit. Private list appearance writes use `REMStore.fetchListWithObjectID`, `REMSaveRequest.updateList`, `REMListChangeItem.setColor`, and `REMListChangeItem.appearanceContext.setBadgeEmblem` / `setBadge`, then save through ReminderKit. `list-pin` and `list-unpin` use `REMListChangeItem.setIsPinned` for regular lists and `REMSmartListChangeItem.setIsPinned` for smart lists. Groceries list metadata writes use `REMListChangeItem.groceryContextChangeItem`, `setShouldCategorizeGroceryItems`, and `setGroceryLocaleID`. For item sorting, RemCTL first polls section membership rows because Reminders auto-sorts new Groceries items; only unsectioned items fall back to `categorizeGroceryItemsWithReminderIDs`. `--symbol` is restricted to official emblem names because arbitrary SF Symbol strings are accepted by the private API but render as the default icon in Reminders. Custom icons should use `--emoji`.

## Smart Lists

Smart lists are stored in `ZREMCDBASELIST` as `REMCDSmartList` rows (`Z_ENT = 4`) with `ZSMARTLISTTYPE` and optional `ZFILTERDATA`.

`smart-lists` is read-only. It reports built-in and custom smart lists with numeric row ID, object UUID, smart-list type, pin state, pin date, filter length, and decoded filter summaries where possible. Current custom smart-list filters on macOS 26 store `ZFILTERDATA` as UTF-8 JSON bytes. The decoder also accepts keyed-archive research samples shaped as `ReminderKitInternal.REMCustomSmartListFilterDescriptor` with a `data` field containing the same JSON bytes.

`smart-list-create` is private and experimental. Python validates and encodes the Reminders filter payloads decoded from Reminders.app, but the user-facing write path only allows shapes verified to materialize in Reminders.app: Any Tag, date any/today/on/before/after/range, time of day, priority single or Priority: Any, flagged, vehicle connected, specific location, one included list, and top-level all/any matching across those families. Known decoded shapes that write as zero filters are rejected before saving: selected tags, untagged, no-date, relative date, no-time, vehicle disconnected, list exclusions, and more than one included list. RemCTL base64-encodes the raw JSON bytes and sends them to `remctl-private` alongside optional private appearance metadata. The helper resolves the active CloudKit account, verifies `supportsCustomSmartLists`, creates the smart list with `REMSaveRequest.addCustomSmartListWithName`, explicitly attaches the change item to the account, sets the custom smart-list supported-version fields to `20220430`, sets `smartListType`, `filterData`, color, and badge appearance when requested, and saves through ReminderKit. The account ownership fields keep the object durable; the supported-version fields are required for Reminders.app to materialize the filter controls instead of showing zero filters. `smart-list-edit` fetches a custom smart list by object ID and replaces `filterData` and/or private appearance metadata through `REMSaveRequest.updateSmartList`. `smart-list-delete` fetches a custom smart list by object ID, removes it from the parent account through ReminderKit, and never matches built-in smart lists.

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

Reminder JSON keeps due dates and alarm/display dates separate for agents. `dueDate` is serialized from `ZREMCDREMINDER.ZDUEDATE`, the actual due date. When Reminders also stores `ZDISPLAYDATEDATE`, for example after adding an EventKit alarm 15 minutes before the due date or when storing an all-day reminder, RemCTL exposes that value as `displayDate` instead of overwriting `dueDate`. In create mode, date-only due inputs such as `today`, `tomorrow`, `2026-06-01`, `+3d`, and `next friday` are sent to the EventKit bridge as all-day reminders using date-only components. On `edit -d`, RemCTL carries a single absolute alarm forward only when it matches the old due time; this keeps Reminders.app's visible time aligned for ordinary reschedules without rewriting deliberately separate custom alarms. On `edit -d clear`, RemCTL clears a single absolute alarm only when it matches the old due/display time.

Normal alarms are `ZREMCDOBJECT` rows linked from the reminder through alarm rows (`Z_ENT = 15`) and trigger rows. Relative triggers serialize as `alarms` entries with `type: "relative"`, `relativeOffset`, `relativeOffsetMinutes`, and a human label. Absolute/date-component triggers serialize with `type: "absolute"`, `dateComponents`, and a best-effort local `date` string. Private ReminderKit location alarms use the same alarm relationship with location trigger rows and serialize as `type: "location"` plus a `location` object.

## Flags, Urgent, and Early Reminders

Flags are read from `ZFLAGGED` and shown as `⚑`.

macOS 26 urgent reminders are read from `ZISURGENTSTATEENABLEDFORCURRENTUSER` and shown as `⏰`. Apple describes urgent reminders as reminders that schedule an alarm when due. Normal writes do not touch the private urgent fields; `add --private --urgent` and `edit --private --urgent` can write them through the unsupported private helper.

Early Reminders are private due-date delta alerts. Reminders stores them in `ZREMCDDUEDATEDELTAALERT` and mirrors a JSON envelope in `ZREMCDREMINDER.ZDUEDATEDELTAALERTSDATA`; the live iOS/macOS UI value “15 minutes” is `dueDateDeltaUnit = 0` and `dueDateDeltaCount = -15`. RemCTL reads that blob into `earlyReminder`/`earlyReminders` JSON fields and writes through `REMReminderChangeItem.dueDateDeltaAlertContext`, not EventKit. Replacement writes remove existing due-date delta alert identifiers before adding the new `REMDueDateDeltaInterval`, because simply adding a new alert can leave multiple early alerts attached to the same reminder.

## Permissions

The CLI process may need Full Disk Access for the app or Python interpreter running `remctl`.

TCC grants are context-specific. Terminal, Codex, a CI runner, and another host app can each have different access to the same Reminders database. A green report from Terminal does not prove that an agent context can read the database.

Check setup with:

```bash
remctl doctor --for-agent
```

Open the guided setup flow with:

```bash
remctl permissions full-disk-access
```

The helper opens the Full Disk Access pane, copies the first path to the clipboard, exposes each target as a draggable file row, and periodically checks whether each target can read the Reminders store. Verified targets get a green check. It does not edit macOS TCC data directly.

## Multi-Account Support

macOS Reminders stores each account — iCloud, Exchange, CalDAV, and on-device
local — in a separate `Data-<uuid>.sqlite` file under the Stores directory.
RemCTL reads the account name and type directly from each store's CoreData
metadata so that accounts are identified by the display name Reminders.app
shows, not by a UUID or file path.

**Account discovery**

`discover_accounts()` (remctl:~280) scans the Stores directory and returns all
real accounts in descending order by reminder count. Each `Account` namedtuple
carries `store_path`, `name`, and `type`. The account type is derived from
`ZREMCDREPLICAMANAGER.ZIDENTIFIER` suffixes:

| Identifier suffix | Type |
| --- | --- |
| `com.apple.exchangesync.exchangesyncd` | Exchange |
| `com.apple.reminders` | iCloud |
| (none or other) | Local |

Accounts named `LocalInternal` and stores that contain no reminders are
excluded automatically. The first element of the returned list is always the
same store that the existing `find_main_db_path()` would have selected, so
the default single-account behavior is unchanged.

**Default database selection**

`find_main_db_path()` (remctl:~226) picks the store with the most reminder
rows, using the most-recent file-group modification time (`.sqlite`, `-wal`,
and `-shm` files) as a tiebreak. This correctly identifies the active iCloud
store even when a larger but less active Exchange store is present.

**Account scope in commands**

Read commands (`lists`, `show`, `today`, `flagged`, `urgent`, `upcoming`,
`overdue`, `search`) accept `--all-accounts` and `--account NAME`. Write
commands (`add`, `done`, `undone`, `edit`, `delete`, `flag`, `unflag`) accept
`--account NAME` to target a reminder or list in a specific account when the
same integer ID (`Z_PK`) exists in more than one store.

Account flags are accepted both before and after the subcommand (the launcher
skips them when identifying the command), so `remctl --account X today` and
`remctl today --account X` are equivalent.

`resolve_account_scope()` resolves the active scope from, in priority order:
`--account`/`--all-accounts` flags, the `REMCTL_ACCOUNT_SCOPE` environment
variable, the `accountScope` key in `~/.config/remctl/config.json`, and the
single-account default. When the scope resolves to exactly one account, all
output is byte-identical to the pre-multi-account behavior — no account labels,
no extra columns.

**Z_PK collision safety**

`Z_PK` row identifiers are local to each store and are not unique across
accounts. RemCTL never mixes rows from different stores into a single query
or join. The `iter_account_dbs()` context manager opens one connection per
account and keeps them separate through serialization; account metadata is
stamped onto already-serialized dicts, not injected into SQL queries.

For single-item commands, `_resolve_reminder_for_write()` honors the active
scope: with one account it reads that store directly; across multiple accounts
it scans each store for the requested ID, acts on a unique match, and reports an
ambiguity error when the ID exists in more than one. `resolve_list_ref_across()`
and `resolve_reminder_across()` provide the same scan-and-disambiguate logic for
list and reminder references. All three stamp the resolved account back onto the
request so the write path targets the correct store.

**Writes to non-CloudKit accounts**

The EventKit bridge enumerates calendars from every account, so writes are not
limited to iCloud. iCloud reminders carry a CloudKit identifier
(`ZCKIDENTIFIER`) that RemCTL passes straight to the bridge. Exchange and other
CalDAV reminders do not store that identifier, so `_ek_identifier()` resolves a
stable EventKit `calendarItemIdentifier` on demand: it asks the bridge
(`list_calendars`) for the target calendar, then (`find_reminder`) for the
reminder by title within that calendar. New reminders are likewise created
against a resolved `calendarIdentifier` rather than a list name, which keeps
same-named lists in different accounts unambiguous. Because non-iCloud
reminders have no CloudKit identifier, the numeric ID of a freshly created
Exchange reminder is recovered by title immediately after creation.

The config file additionally accepts `storeDir` and `dbPath` keys, the
persistent equivalents of `REMCTL_STORE_DIR` and `REMCTL_DB`; environment
variables take precedence over the config file.

## Environment Overrides

```bash
REMCTL_BRIDGE_PATH=/path/to/remctl-bridge
REMCTL_PRIVATE_PATH=/path/to/remctl-private
REMCTL_PERMISSIONS_PATH=/path/to/remctl-permissions
REMCTL_PATH=/path/to/remctl
REMCTL_STORE_DIR=/path/to/reminders/store   # override the whole Stores directory
REMCTL_DB=/path/to/Data-XXXX.sqlite         # pin a specific database file (bypasses auto-detection)
REMCTL_ACCOUNT_SCOPE=all                    # equivalent to --all-accounts for every command
REMCTL_ACCOUNT_SCOPE=iCloud                 # restrict to a named account globally
REMCTL_CONFIG_DIR=/path/to/config
NO_COLOR=1
```
