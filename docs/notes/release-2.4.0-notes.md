# RemCTL 2.4.0

RemCTL 2.4.0 brings subtask nesting and Smart List sections, and fixes every open bug report and contributor pull request. Thanks to everyone who reported issues and sent fixes.

**New**

- Make an existing reminder a subtask, turn a subtask back into an ordinary reminder, or reorder subtasks, without losing its ID or details: `reminder-move ID --parent PARENT_ID --private` and `reminder-move ID --top-level --private`. Thanks to @danielstengler-ai ([#63](https://github.com/viticci/remctl/pull/63)).
- Put a reminder in a section of a custom Smart List with `edit ID --private --smart-list "Flagged Tasks" --section "Next Up"`. `smart-lists` shows each Smart List's sections and IDs, and MCP `create_reminder` and `update_reminder` take `smart_list`. Thanks to @davidgliu ([#61](https://github.com/viticci/remctl/pull/61)) and @marcodegra5 ([#75](https://github.com/viticci/remctl/issues/75)).

**Fixes**

- macOS 15: alarms, repeat rules, assignments, and sharees are read correctly again. Thanks to @NSExceptional ([#74](https://github.com/viticci/remctl/pull/74)).
- A crashed Capability Host no longer leaves an orphaned broker that blocks every new host. Thanks to @tkaufmann ([#66](https://github.com/viticci/remctl/issues/66)).
- The guided Full Disk Access helper accepts hosts built from source with RemCTL's local certificate. Thanks to @marcodegra5 ([#67](https://github.com/viticci/remctl/issues/67)).
- Flagging no longer launches Reminders.app when the private helper is available. Thanks to @mymatejackson ([#65](https://github.com/viticci/remctl/issues/65)).
- Subtasks keep their order when created, moved, listed, or moved to another list ([#63](https://github.com/viticci/remctl/pull/63)).
- MCP results stay under 48 kB and say what was trimmed; write timeouts report an unknown outcome instead of inviting duplicate retries; `doctor` gives the same verdict in Terminal and in MCP services; sections of deleted lists are hidden; and both plugins find RemCTL in `~/.local/bin`. Thanks to @davidmaxschmidt ([#68](https://github.com/viticci/remctl/pull/68), [#69](https://github.com/viticci/remctl/pull/69), [#70](https://github.com/viticci/remctl/pull/70), [#71](https://github.com/viticci/remctl/pull/71), [#72](https://github.com/viticci/remctl/pull/72), [#73](https://github.com/viticci/remctl/pull/73)).
- The host starts when launchd holds back its launch. Thanks to @evaneus ([#64](https://github.com/viticci/remctl/pull/64)).
- Source builds find their local signing certificate on Macs where `codesign` ignores an explicit keychain. Thanks to @tashmurra ([#62](https://github.com/viticci/remctl/pull/62)).
- The installer copies the app out of the downloaded disk image before the administrator step, which failed on macOS 27.2.

See the [changelog](https://github.com/viticci/remctl/blob/main/CHANGELOG.md) for details.

Download `RemCTL-arm64.dmg`, open it, and run **Install RemCTL**. Existing installs keep their macOS permission grants. Restart Claude Code and Codex conversations to pick up the plugin update.
