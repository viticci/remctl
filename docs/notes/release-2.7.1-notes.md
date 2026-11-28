# RemCTL 2.7.1

RemCTL 2.7.1 lets When Messaging a Person take a name from Contacts, and fixes clearing a section in the Codex workspace.

**New**

- **Messaging contacts by name.** `--messaging-contact "John Voorhees"` looks the person up in Contacts and uses all of their phone numbers and email addresses. It needs exactly one matching contact and checks before writing anything. If several contacts match, it lists them so you can pick one by identifier. Phone numbers and email addresses still work as before. MCP `messaging_contact` takes names too. Thanks to @pflajszer (#83).
- **Contacts is an optional permission.** Run `remctl permissions contacts` once. RemCTL Capability Host shows its own permission window, then macOS asks. Setup doesn't ask for Contacts, and `doctor` reports it without a warning. RemCTL reads only the matching person's phone numbers and email addresses, and only when you set When Messaging a Person by name.

**Fixes**

- In the Codex workspace, Move to Section > None, dropping a card in the Others column, and clearing a reminder's section in the inspector failed. They now take the reminder out of its section.
- The workspace's command-palette forms work with the 2.7.0 fields, such as replacing a rich link or leaving a section, instead of failing with "needs private: true".
- The splash screen, help, README, guides, workspace guides, and skills cover everything new in 2.7.0 and 2.7.1.

See the [changelog](https://github.com/viticci/remctl/blob/main/CHANGELOG.md) for details.

Download `RemCTL-arm64.dmg`, open it, and run **Install RemCTL**. Existing installs keep their macOS permission grants. Restart Claude Code and Codex conversations to pick up the plugin update.
