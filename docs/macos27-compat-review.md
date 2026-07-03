# macOS 27 Golden Gate Compatibility Review

Date: 2026-06-12

Host:

- macOS 27.0 Golden Gate, build 26A5353q
- Darwin 27.0.0
- RemCTL branch: `codex/macos27-compat-review`

## Public Release Notes

Apple's macOS 27 beta release notes were checked on 2026-06-12. The page mentions SDK, security, plugin, Charts, and SwiftUI `@State` changes, but no Reminders, EventKit, or ReminderKit-specific compatibility changes surfaced in the scraped release notes.

## Local Store Shape

Active store:

```text
/Users/viticci/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/Data-26773582-F8E5-4444-A416-16E4FE11586A.sqlite
```

Observed schema:

- 26 SQLite tables.
- 51 CoreData entities in `Z_PRIMARYKEY`.
- Core tables RemCTL relies on are present: `ZREMCDREMINDER`, `ZREMCDBASELIST`, `ZREMCDBASESECTION`, `ZREMCDOBJECT`, `ZREMCDHASHTAGLABEL`, `ZREMCDSAVEDREMINDER`, `ZREMCDSAVEDATTACHMENT`, and `ZREMCDTEMPLATE`.
- Automated comparison found no missing table/column pairs for RemCTL's current SQL references.

macOS 27 fields/tables worth tracking:

- `ZREMCDBASELIST` includes grocery cache/predefined-section fields:
  - `ZCACHEDGROCERYITEMSCOUNT`
  - `ZMEMBERSHIPSOFREMINDERSINPREDEFINEDGROCERYSECTIONSCHECKSUM`
  - `ZMEMBERSHIPSOFREMINDERSINPREDEFINEDGROCERYSECTIONSASDATA`
- `ZREMCDDUEDATEDELTAALERT` is populated and mirrors Early Reminder metadata by reminder UUID blob.
- Existing active Early Reminder rows still had `ZREMCDREMINDER.ZDUEDATEDELTAALERTSDATA`, so current readback was already working before the patch.

## Read Compatibility

These read paths returned valid JSON on macOS 27:

- `today`, `upcoming`, `overdue`, `flagged`, `urgent`
- `lists`, `sections`, `tags`
- `show Work`
- `smart-lists`
- `templates`
- `search`
- limited EventKit fallback reads for `today`, `upcoming`, and `search`

Direct SQLite and EventKit fallback counts matched for `today` and `upcoming` in this context.

> Follow-up: a later fix aligned the `stats` overdue count with the `overdue`/`today` semantics (display-date for all-day reminders, start-of-day cutoff, and excluding orphaned reminders).

## Write Compatibility

The live EventKit edit matrix passed all 8 cases:

- matching absolute alarm moves with due date
- custom absolute alarm survives reschedule
- no-op reschedule preserves custom alarm
- due clear removes matching absolute alarm
- alarm clear removes absolute alarm
- relative alarm survives due reschedule
- title/notes/priority preserve schedule
- list move preserves schedule

The live private ReminderKit matrix passed all covered paths:

- private helper availability
- guardrails for private-only metadata
- list create/edit/pin/unpin with private appearance
- Groceries list creation and grocery item categorization
- rich reminder metadata: URL, tags, section, subtask, image, urgent, flag, Early Reminder
- location alarm write through the EventKit bridge
- smart-list create/edit/pin/unpin across supported filter families
- template create/apply

Both matrices cleaned up their disposable data; follow-up searches found no remaining `macOS27 Audit`, `macOS27 Patch Audit`, or `macOS27 Main Push Audit` reminders, lists, smart lists, or templates.

## Local Patch

Two compatibility hardening changes were made:

1. `lists --json` now exposes the optional grocery cache/predefined-section metadata when present.
2. Early Reminder readback/removal now falls back to `ZREMCDDUEDATEDELTAALERT` if `ZDUEDATEDELTAALERTSDATA` is absent or stops being mirrored in a future macOS 27 build.

This keeps the existing blob path as primary and only uses the normalized table when needed.

> Follow-up: a later fix wired the `info` text output to the same `ZREMCDDUEDATEDELTAALERT` fallback, so both text and `--json` now show Early Reminders when only the normalized table is populated (previously only `--json` did).

## Verification

Commands run successfully after the patch:

```bash
python3 -m unittest discover -s tests
python3 scripts/live_edit_matrix.py --remctl ./remctl --prefix "macOS27 Patch Audit ..."
python3 scripts/live_private_matrix.py --remctl ./remctl --prefix "macOS27 Patch Audit ..."
./install.sh --doctor
remctl today --json
remctl lists --json
remctl smart-lists --json
remctl templates --json
python3 scripts/live_edit_matrix.py --remctl remctl --prefix "macOS27 Main Push Audit ..."
python3 scripts/live_private_matrix.py --remctl remctl --prefix "macOS27 Main Push Audit ..."
```

Results:

- Unit tests: 200 passed.
- EventKit live matrix: passed.
- Private ReminderKit live matrix: passed.
- Installed CLI doctor after the zsh `fpath` fix: 11 checks, 0 warnings, 0 failures. Current RemCTL also reports an explicit `eventkit` write-access check, so modern healthy runs include that additional check.

## Current Take

RemCTL is compatible with this macOS 27 Golden Gate beta build. No breaking Reminders database, EventKit, or ReminderKit change showed up in local testing. The only observed schema additions worth acting on now are defensive: expose grocery cache metadata and use the normalized Early Reminder table as a fallback.
