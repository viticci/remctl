# Private Metadata Writes

RemCTL's normal write path is EventKit via `remctl-bridge`. Private metadata writes are different: they use Apple's private ReminderKit framework through `remctl-private`.

This mode is unsupported by Apple, optional, and explicit. Use `--private` on `add` or `edit`.

RemCTL still does not write directly to SQLite.

## Safety Model

`remctl-private` receives a bounded JSON payload on stdin, looks up the target reminder through ReminderKit, applies one of a fixed set of actions, and saves through Apple's Reminders stack. It does not spawn a shell, does not accept arbitrary Objective-C selectors, and does not mutate the SQLite store.

The helper is still experimental. It links a private framework, so Apple can rename classes, change method signatures, reject behavior, or alter sync semantics in any macOS update. Public builds should treat this as an opt-in power-user feature, not a stability guarantee.

## Supported

Verified on macOS/iCloud sync:

- web rich URL attachments: `--private --url https://example.com`
- synced tags: `--private -t remctl,work`
- section assignment: `--private --section "Research"`
- section creation and assignment: `--private --new-section "Research"`
- subtasks: `--private --subtask "Follow up"`
- image attachments: `--private --image ~/Desktop/mockup.png`
- real flag state: `edit ID --private --flagged` or `add ... --private -f`
- urgent state: `edit ID --private --urgent`
- location alarms: `edit ID --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090`

Not exposed:

- generic file/PDF attachments. They are rejected because Reminders does not reliably display them.
- raw SQLite writes. Earlier experiments proved direct row insertion can stay local-only and fail to sync.

## Create Examples

```bash
remctl add "Research" -l Projects --private \
  --url "https://example.com" \
  -t remctl \
  --section "Research"

remctl add "Prepare screenshots" -l Projects --private \
  --image ~/Desktop/mockup.png \
  --subtask "Export final PNG"

remctl add "Flagged private task" -l Work --private -f
```

With `--private`, `--url` creates a web rich link attachment and `-t/--tags` creates real synced Reminders tags. Without `--private`, `--url` is appended to notes and `-t/--tags` appends inline hashtags to the title.

## Edit Examples

```bash
remctl edit 23880 --private --url "https://example.com"
remctl edit 23880 --private -t remctl,work
remctl edit 23880 --private --section "Research"
remctl edit 23880 --private --new-section "Inbox Zero"
remctl edit 23880 --private --subtask "Follow up"
remctl edit 23880 --private --image ~/Desktop/mockup.png
remctl edit 23880 --private --flagged --urgent
remctl edit 23880 --private --no-flagged --no-urgent
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200 --proximity arriving
```

## Guardrails

Private-only options fail before writes unless `--private` is set.

Examples of rejected commands:

```bash
remctl add "Research" -l Projects --section "Research"
remctl edit 23880 -t remctl
remctl edit 23880 --urgent
```

These fail because they would otherwise look successful while silently dropping private metadata.

`--private --url` accepts `http` and `https` URLs. Image attachments must point to readable image files. Location alarms validate latitude, longitude, radius, and proximity before saving.

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

Agents must verify private writes with `remctl info ID --json` and, when sync behavior matters, ask Federico to check another device. Do not assume a CloudKit-clean row means the Reminders UI displays it; generic files and PDFs were the counterexample and are intentionally rejected.
