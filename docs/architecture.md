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

RemCTL opens the database read-only. It never writes to SQLite.

## Writes

Writes go through Apple-supported APIs:

1. `remctl-bridge` writes via EventKit. This is the normal path for create, edit, complete, delete, recurrence, alarms, URLs appended to notes, and list management.
2. AppleScript is a fallback for operations that still need Reminders.app automation behavior.

There is also an explicitly unsupported opt-in helper:

3. `remctl-private` writes selected private metadata through Apple's private ReminderKit framework. It is gated by `--private`, never writes SQLite directly, and is intentionally excluded from normal write behavior. Verified private writes include web rich URL attachments, hashtag labels, section assignment/creation, rich subtasks, image attachments, real flag state, urgent state, location alarms, and list appearance metadata such as exact colors, private emblem names, and emoji badges. For rich subtasks, `remctl-private` creates the child and applies private child metadata, then `remctl-bridge` applies public child fields such as notes and due dates. Generic file/PDF attachments are rejected because Reminders does not reliably show them even when private rows sync.

The bridge is detected next to the installed CLI. Override it with:

```bash
REMCTL_BRIDGE_PATH=/path/to/remctl-bridge remctl add "Test"
```

Override the private helper path with:

```bash
REMCTL_PRIVATE_PATH=/path/to/remctl-private remctl edit 123 --private --url https://example.com
```

See [private-metadata.md](private-metadata.md) for supported private fields, known limits, and verification rules.

## List Appearance

Reminders stores list appearance on `ZREMCDBASELIST`:

- `ZCOLOR` is a keyed archive containing a `REMColor` object with symbolic color names, hex, and RGB values.
- `ZBADGEEMBLEM` is text. Emoji badges are stored as JSON such as `{"Emoji":"📌"}`; Reminders picker icons are stored as private emblem names such as `education3`.

RemCTL reads these fields directly for display. Normal `list-create --color NAME` still writes through EventKit. Private list appearance writes use `REMStore.fetchListWithObjectID`, `REMSaveRequest.updateList`, `REMListChangeItem.setColor`, and `REMListChangeItem.appearanceContext.setBadgeEmblem` / `setBadge`, then save through ReminderKit. Arbitrary emblem strings can be saved, but Reminders may not render every SF Symbol name in the UI.

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

## Flags and Urgent Reminders

Flags are read from `ZFLAGGED` and shown as `⚑`.

macOS 26 urgent reminders are read from `ZISURGENTSTATEENABLEDFORCURRENTUSER` and shown as `⏰`. Apple describes urgent reminders as reminders that schedule an alarm when due. Normal writes do not touch the private urgent fields; `edit --private --urgent` can write them through the unsupported private helper.

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

## Environment Overrides

```bash
REMCTL_BRIDGE_PATH=/path/to/remctl-bridge
REMCTL_PRIVATE_PATH=/path/to/remctl-private
REMCTL_PERMISSIONS_PATH=/path/to/remctl-permissions
REMCTL_PATH=/path/to/remctl
REMCTL_STORE_DIR=/path/to/reminders/store
REMCTL_CONFIG_DIR=/path/to/config
NO_COLOR=1
```
