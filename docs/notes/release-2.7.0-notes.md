# RemCTL 2.7.0

RemCTL 2.7.0 can replace a reminder's rich link, take a reminder out of its section, read and set up custom smart-list boards, and set "When Messaging a Person". It also makes a broken install repairable.

**New**

- **Replace or remove rich links.** `edit --private --set-url URL` replaces every rich link with one, for example after an agent's linked note was renamed, and `--clear-urls` removes them. `--url` still adds a link. MCP: `set_url` and `clear_urls` on `update_reminder`. Thanks to @rayshan (#79).
- **Leave a section.** `edit --private --no-section` puts a reminder back in the unsectioned part of its list, the same change as Reminders' Move to Section > None. Add `--smart-list` to take it out of a custom smart list's section instead. MCP: `no_section`. Thanks to @nutcracker (#81).
- **Smart lists as boards.** `info`, `subtasks`, and `export` JSON report `smartListSections`, so a script can tell which column a card is in after you drag it in Reminders. `show --smart-list NAME` groups a custom smart list's reminders by its sections. `section-create --smart-list` and `edit --smart-list … --new-section` set up columns without opening Reminders. Thanks to @pflajszer (#82).
- **When Messaging a Person.** `--messaging-contact PHONE_OR_EMAIL` on `add` and `edit` makes the reminder appear the next time you message that person in Messages; `--clear-messaging-contact` removes it. Reads report `messagingContact`. RemCTL takes phone numbers and email addresses, not names. MCP: `messaging_contact` and `clear_messaging_contact`. Thanks to @pflajszer (#83).

**Fixes**

- After some of RemCTL's files changed, the installer told you to move them out, then refused again because it still expected them. Moved files no longer block it, and in Terminal it offers to move changed files to the Trash. The uninstaller works even when the protected Python under `/Library/RemCTL` is missing, and a source build over the download explains the certificate mismatch. The installation guide has a new Repairing a broken install section. Thanks to @adamayers (#84).

The new writes need `private: true` (or `--private`) and the 2.7.0 helper, which RemCTL checks before writing anything.

See the [changelog](https://github.com/viticci/remctl/blob/main/CHANGELOG.md) for details.

Download `RemCTL-arm64.dmg`, open it, and run **Install RemCTL**. Existing installs keep their macOS permission grants. Restart Claude Code and Codex conversations to pick up the plugin update.
