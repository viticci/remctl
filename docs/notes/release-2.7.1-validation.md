# RemCTL 2.7.1 release validation

RemCTL 2.7.1 lets When Messaging a Person take a name from Contacts, adds Contacts as an optional Capability Host permission, fixes clearing a section in the Codex workspace, and brings the docs, help, splash screen, and skills up to date. The release was built from commit `e6a3a92`.

## What changed

- The bridge has a `contacts_lookup` action. It reads Contacts with `CNContactStore` only when access is already allowed, and never asks for it. Without access it answers `contacts_access_required`. It returns each match's identifier, full name, phones, and emails.
- `--messaging-contact` treats a value that isn't a phone or email as a name or contact identifier. It needs exactly one contact with a phone or email, preferring an exact full-name match, and resolves the name in `validate_private_args`, before any write. A value splits on commas only when every part is a phone or email, so "Voorhees, John" stays one name.
- The Capability Host has a `contacts` permission: `PermissionPrompt.contacts`, the `requestContacts` native operation, and a `contacts` key in its permission payload, which the broker validates. `remctl permissions contacts` uses the same gate window as Reminders and Automation. It refuses direct mode, and with `--json` it only reports. `onboard` doesn't request Contacts. `doctor` reports it as an OK line.
- Info.plist has `NSContactsUsageDescription`. Release builds sign the host and the bridge with `com.apple.security.personal-information.addressbook`.
- The Codex workspace sent `section: "none"` in four places. RemCTL refused it with "section not found in target list: none", which was confirmed on the installed CLI. It now sends `no_section: true`. Palette forms send `private: true` for the 2.7.0 fields, `grocery`, `location_title`, and list changes. Section Create shows its name field next to a smart list.
- The splash screen, `add`/`edit`/`smart-lists`/`permissions` help, completions, README, installation, commands, private-metadata, MCP, architecture, Hermes, and workspace guides, and both skills now cover 2.7.0 and 2.7.1.

## Permission flow

`remctl permissions contacts` showed the host's "RemCTL Permissions" window ("Allow RemCTL to look up your contacts"), then the macOS prompt "Allow 'RemCTL Capability Host' to access your contacts?" with the usage text. Federico reviewed screenshots of both and granted access. The prompt stays on screen after RemCTL's window times out, and a later answer still applies. Screenshots are in `~/.tstack/evidence/remctl/2026-11-28-contacts-271/`.

## Regression checks

- 988 tests across the M3 Ultra and M5 Ultra. Two tests that sign with a real identity fail on the M3 because its Keychain is locked over SSH; both pass on the M5. After the last review fixes, the quick suites passed on the M5: `test_cli` 488, `test_capability_transport` 87, `test_desktop_plugin` 26, `test_mcp_typed_tools` 17.
- The host typechecks with and without `REMCTL_TESTING`. The bridge compiles with Contacts. UI type checks and interaction contracts pass. Zsh and bash completions parse.

## Review

Gemini 4 Neon (extra-high thinking) reviewed `4c7e713` and found six issues:

- Important: a name with a comma was split into two lookups, which could merge two people's handles.
- Important: `permissions contacts` ignored direct mode and prompted under `--json`.
- Pre-existing: the workspace Section Create form hid its name.
- Pre-existing: some palette forms omitted `private: true`.
- Nit: a misplaced installation paragraph.
- Nit: outdated help and completion text.

All six were fixed. A second round confirmed each one and found two small regressions in the fixes: a stray trailing comma on a handle, and a lost host error under `--json`. Both were fixed in `e6a3a92`.

## Installed and live acceptance

This Mac was reinstalled from source with its Developer ID identity and existing protected Python, so no administrator password was needed. The Contacts grant carried over across reinstalls. Doctor reports 17 checks, zero failures, and two warnings: Automation, which macOS can't report until Reminders.app has run. The installed host's CDHash is `c0f52f5b7404727bcdde487e5395905e4f29817c`. The installed-surface audit passes all 41 comparisons, including both plugin caches at 2.7.1. The Tailscale endpoint reports 2.7.1. The installer's reload check timed out under heavy load, and `remctl mcp status` reported it healthy.

| Case | Observed result |
| --- | --- |
| `--messaging-contact "John Voorhees"`, without access | `contacts_access_required`; the title and contact didn't change |
| Same, after Federico allowed access | One contact, 1 phone and 5 emails, verified |
| `--messaging-contact "Voorhees, John"` | One contact: John Voorhees |
| `--messaging-contact "John"` | `contact_ambiguous` with 4 candidates |
| `permissions contacts --json` | Reported `authorized` without prompting |
| Two test reminders for John (Reminders list) | Reminders.app shows "Messaging: John Voorhees" on both |
| Workspace `update_reminder` with `no_section`, through plugin-mode `workspace_mutate` | Left the section, verified; `section none` fails as before |

## Signed and notarized artifact

Both apps and the disk image were accepted by Apple, stapled, and validated. A quarantined copy of the disk image passes Gatekeeper as Notarized Developer ID, and so do both apps inside it. The release host and bridge carry the addressbook entitlement.

| Artifact | Verification value |
| --- | --- |
| App submission | `b1b2fd0b-2368-4180-853d-7c211691ae98` — Accepted |
| Disk image submission | `f7b08a5d-0846-4ffa-889a-5e6055bc34d2` — Accepted |
| `RemCTL-arm64.dmg` SHA-256 | `09bcefdce39704822b9b70693ad6f229bb7250a3c89ee8c822bc48def67e7b18` |
| Release host CDHash | `b0c835b4e6d39993d7f040fae61839c16484c2c6` |
| Bundled Python | `3.13.15` |
| Bundled protected runtime ID | `25ddf1bc45ec0507061a9e0f29aa889ce8c12b30ea09389e727b699130008f3b` |

## Coverage limits

The Contacts lookup was proven on the source-built host on this Mac, which isn't signed with the hardened runtime. The release build's hardened-runtime Contacts entitlement was checked in its signature, not exercised by a lookup, because installing the disk image's own runtime needs an administrator password. The reviewer confirmed that a hardened-runtime bridge with the entitlement answers `contacts_access_required` correctly without prompting. The workspace fix was exercised through the plugin's `workspace_mutate` path, not by clicking in Codex.

## Publication

Published on November 28, 2026. The `v2.7.1` tag points to commit `7361bbe`, which adds the release notes and this validation on top of the build commit `e6a3a92`; the disk image was built from `e6a3a92`. The [public release](https://github.com/viticci/remctl/releases/tag/v2.7.1) is the latest release, not a draft or prerelease. The disk image was downloaded without authentication through the `latest` URL: its bytes, GitHub's asset digest, and SHA-256 (`09bcefdce39704822b9b70693ad6f229bb7250a3c89ee8c822bc48def67e7b18`) match the verified build, its stapled ticket validates, and a quarantined copy passes Gatekeeper as Notarized Developer ID. `scripts/download_release.sh --dry-run` also verified it.
