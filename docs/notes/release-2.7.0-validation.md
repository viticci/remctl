# RemCTL 2.7.0 release validation

RemCTL 2.7.0 builds four feature requests and fixes an install-recovery bug: replacing and removing rich links (#79), leaving a section (#81), custom smart lists as boards (#82), "When Messaging a Person" (#83), and repairing a broken install (#84). The release was built from commit `045e23f` ("Check smart-list section targets before writing, per review").

## What changed

- The private helper reports protocol 5 and has six new actions: `set_url_attachments`, `remove_from_section`, `remove_from_smart_list_section`, `create_smart_list_section`, `add_smart_list_section_and_assign`, and `set_messaging_contact`. The Capability Host accepts helpers that report 2 through 5. Every new write checks for protocol 5 before the first write.
- `edit --set-url` and `--clear-urls` call `removeURLAttachments`, then add the new link in the same save.
- `--no-section` writes a membership with no `groupID`. The issue suggested `isObsolete: YES`, and the first build wrote that, but Reminders.app kept showing the reminder in its section, even after a relaunch. Reminders' own Move to Section > None writes a membership with no `groupID`, which was confirmed by reading the store after using it. Federico's store had 183 such entries and no `isObsolete` entries.
- Smart-list sections are created with `addSmartListSectionWithDisplayName:toSmartListSectionContextChangeItem:`, after the existing ones. `smartListSections` is read from each custom smart list's `ZMEMBERSHIPSOFREMINDERSINSECTIONSASDATA`.
- The messaging trigger is `REMReminderChangeItem.contactHandles`, a `REMContactRepresentation` of phones and emails, stored as a keyed archive in `ZCONTACTHANDLES`. The store has no contact-trigger alarm entity. RemCTL takes handles, not names, so it needs no Contacts permission.
- The installer treats a manifest entry whose file is gone as fine, and offers in Terminal to move changed files to the Trash. The uninstaller accepts the same state and falls back to the app's own Python when `/Library/RemCTL/Python/<id>` is missing. `build_distribution.py --preserve-identity` stops with an explanation when the recorded certificate isn't in the keychain.

## Reproducing #84

In the installer's temp-prefix harness on the M3 Ultra, before the fix: install, edit `remctl_runtime.py`, move it out of `bin` as the error message said, then run `uninstall.sh --dry-run`. It refused with "Capability-host artifacts exist without an exact installer ownership marker", which is the state the reporter described. The two new lifecycle tests failed on the unfixed scripts and pass after the fix.

## Regression checks

- 986 tests on the M3 Ultra with Python 3.14. Every test passed except two that sign with a real identity, which fail there with `errSecInternalComponent` because the M3's Keychain is locked over SSH. Both passed on the M5 Ultra.
- On the M5 Ultra, the quick suites passed after the review fixes: `test_cli` 487, `test_capability_transport` 87, `test_mcp_server` 102, `test_desktop_plugin` 26, `test_mcp_typed_tools` 17, `test_smart_lists` 14, `test_serialization` 9, and `test_mcp_budget` 8.
- The helper compiles with `-Wall` and no warnings. The UI was rebuilt, so both plugin manifests carry build identifier `f6e03e6032d8b644`.

## Review

Gemini 4 Neon (extra-high thinking) reviewed commit `9231f3b` and reported seven Important findings and two Nits. Two were tests the author had already fixed in `19ac74d`. The others were fixed in `045e23f`:

- A bad `--smart-list` with `--no-section` or `--new-section` was caught after the EventKit write.
- A duplicate or blank `--new-section` name was accepted for a smart list.
- `show --smart-list` printed a traceback for undecodable filters.
- `show` and `section-create` accepted both `--smart-list` and `--smart-list-id`.
- `--no-section` read sections strictly and could treat an unreadable store as verified.
- `#` was stripped from messaging handles.
- `--via-eventkit` and `--format table` with `--smart-list` gave misleading errors.

A second round confirmed all nine fixes by rerunning its reproductions. It left two Nits, both a SQLite lock during the read-back after a `--no-section` write: one smart-list query that still swallows the error, and an `open_db` failure that surfaces as a traceback. Neither was fixed in this release.

## Installed and live acceptance

This Mac was reinstalled from source with its existing Developer ID identity and protected Python runtime, as for 2.2.1 through 2.6.0, so no administrator password was needed. Doctor reports 16 checks, zero warnings, and zero failures. The installed host's CDHash is `37ccc0ffe1da06192e64c66c4d563ac0a52dc799`. The installed-surface audit passes all 41 comparisons, including both plugin caches at 2.7.0. The Tailscale endpoint reports 2.7.0. The third install's reload check timed out once under heavy load, and `remctl mcp status` then reported it healthy.

Live checks used the disposable list `RL Test 270`, the custom smart list `RL Board 270` (tag `rlboard270`), and two reminders titled "RemCTL test - ignore" and "RemCTL test 2 - ignore". Ground truth came from Reminders.app through Cua Driver and from the store itself, read over the Full Disk Access SSH path.

| Case | Observed result |
| --- | --- |
| `--set-url obsidian://…` on a reminder with two links | Store held exactly the new link; `info` showed it |
| `--url` after that | Added a second link |
| `--clear-urls` | Store held no links |
| `--url` with `--set-url` | Refused before writing |
| `--no-section` in a list (first build, `isObsolete`) | RemCTL said verified; Reminders.app still showed the section, so the approach was changed |
| Reminders' Move to Section > None | Store entry with no `groupID` |
| `--no-section` in a list and in a smart list (fixed build) | Store entries with no `groupID`; Reminders.app shows both reminders under Others |
| `section-create --smart-list` ×2, duplicate | Two sections created and shown as Reminders columns; the duplicate was refused |
| `--smart-list --section Doing`, `--new-section Waiting` | Both verified; Reminders.app shows the reminders in Doing and Waiting |
| `info`, `export`, `show --smart-list` | `smartListSections` and the grouping match Reminders.app |
| `--messaging-contact` phone + email | Read back as `messagingContact`; Reminders.app shows the messaging line with the phone |
| `--messaging-contact "Sam Rivera"`, `"#1234"` | Refused |
| MCP `update_reminder`: `set_url`, smart-list `section`, `no_section`, `clear_messaging_contact` + `clear_urls`; `get_reminder` | All succeeded on the installed stdio server; `set_url` without `private` was refused |
| Bad `--smart-list` with a title change | Refused; the title didn't change |

Screenshots are in `~/.tstack/evidence/remctl/2026-11-28-issues-79-84/`.

## Signed and notarized artifact

Both Developer ID apps and the disk image were accepted by Apple, stapled, and validated. A quarantined copy of the disk image passed its checksum and Gatekeeper as Notarized Developer ID, and so did both apps inside it.

| Artifact | Verification value |
| --- | --- |
| App submission | `f7e9eed3-504b-4a27-bff4-e8115b4531b6` — Accepted |
| Disk image submission | `e7b98280-fee1-4541-bd61-e64b63950636` — Accepted |
| `RemCTL-arm64.dmg` SHA-256 | `61e590164c118ba835894ec5c2107e320e2f0af2b57a02819d98699103ee1a48` |
| Release host CDHash | `318f91ff07201299e45faa48b482bed54fa0218c` |
| Bundled Python | `3.13.15` |
| Bundled protected runtime ID | `6cd50ce36d9a072123e4cd41c322393c5a471b8282a2826277c924bb457da209` |

## Coverage limits

Testing ran on Apple silicon with macOS 27.2.1. Only Reminders.app on this Mac was checked, not iPhone, so whether the messaging trigger fires in Messages on iOS wasn't tested. The disk image's own runtime wasn't installed here, because that needs an administrator password. The #84 recovery was reproduced in temp-prefix simulations, not on a Mac whose protected Python had been deleted.

## Publication

Published on November 28, 2026. The `v2.7.0` tag points to commit `b9bb7ca`, which adds the release notes and this validation on top of the build commit `045e23f`; the disk image was built from `045e23f`. The [public release](https://github.com/viticci/remctl/releases/tag/v2.7.0) is the latest release, not a draft or prerelease. The disk image was downloaded without authentication through the `latest` URL: its bytes, GitHub's asset digest, and SHA-256 (`61e590164c118ba835894ec5c2107e320e2f0af2b57a02819d98699103ee1a48`) match the verified build, its stapled ticket validates, and a quarantined copy passes Gatekeeper as Notarized Developer ID. `scripts/download_release.sh --dry-run` also verified it.
