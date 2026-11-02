# RemCTL 2.5.0

RemCTL 2.5.0 lets rich links open apps, moves reminders into shared lists, and lets the Codex workspace fill tall windows. Thanks to everyone who reported these and sent a fix.

**New**

- Rich links can open apps. `--private --url`, subtask URLs, and MCP `url` accept app links such as `obsidian://open?vault=Notes&file=Plan`, which Reminders shows as a link and opens in that app. Web links still need a public host, and `file:`, `javascript:`, `data:`, and file-server schemes stay refused. The Codex workspace shows app links too. Thanks to @rayshan ([#76](https://github.com/viticci/remctl/issues/76)).

**Fixes**

- Moving a reminder into a shared list no longer fails with ReminderKit error `-3002`. RemCTL copies it, with its subtasks, and deletes the original only after the copy reads back the same. The moved reminder gets a new ID, which the result reports. Thanks to @oliverames ([#77](https://github.com/viticci/remctl/pull/77)).
- The Codex workspace fills tall and portrait windows instead of leaving empty space below the list. Thanks to @jrn4z6vmwj-a11y ([#78](https://github.com/viticci/remctl/issues/78)).

See the [changelog](https://github.com/viticci/remctl/blob/main/CHANGELOG.md) for details.

Download `RemCTL-arm64.dmg`, open it, and run **Install RemCTL**. Existing installs keep their macOS permission grants. Restart Claude Code and Codex conversations to pick up the plugin update.
