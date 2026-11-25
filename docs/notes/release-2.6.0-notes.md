# RemCTL 2.6.0

RemCTL 2.6.0 gives AI apps a guide to your reminders, and fixes a hint that could lead an AI app to create the same list twice.

**New**

- RemCTL's MCP server now serves a `reminders` skill: a guide an AI app reads to learn how to work with your Reminders. It covers planning a day, quick capture, triaging overdue reminders, moving, deleting, and restoring reminders, when a change needs `private: true`, and what to check after an uncertain result before trying again. Any app on MCP revision 2026-07-28 that supports skills over MCP can use it. Apps on older revisions see no change.
- In Claude and Claude Code, skills over MCP are a public beta that Anthropic is still turning on account by account. Once the beta is on for your account, the skill appears there as `remctl:reminders`. Until then, nothing changes in those apps.

**Fixes**

- When creating or changing a list timed out, RemCTL told the AI app to check with `search` and `get_reminder`, which never show lists, so the app could create the same list twice. It now says to read the list back with `lists` or `get_list`.

See the [changelog](https://github.com/viticci/remctl/blob/main/CHANGELOG.md) for details.

Download `RemCTL-arm64.dmg`, open it, and run **Install RemCTL**. Existing installs keep their macOS permission grants. Restart Claude Code and Codex conversations to pick up the plugin update.
