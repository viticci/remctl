# RemCTL for Codex

The RemCTL plugin turns Codex on the Mac into a Reminders workspace, with a sidebar that works like the Reminders app. You get list, column, and calendar layouts, an inspector for every field, drag and drop, a command palette, and your real list icons and colors. Apple Reminders stays the source of truth, and everything runs on your Mac through the Capability Host.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/readme/today-dark.jpg">
  <img alt="The RemCTL workspace in Codex, showing Today" src="../assets/readme/today-light.jpg">
</picture>

There are two parts. RemCTL itself (the CLI and the Capability Host) talks to Reminders and holds the permissions. The plugin adds the workspace and the conversation features on top. Installing the plugin alone doesn't install RemCTL or grant any permissions.

## Install

You need:

- RemCTL, installed and passing `remctl doctor`. See [Installation](installation.md).
- Codex for Mac with plugin support, and the `codex` command on your PATH (`codex plugin --help` should work). These steps were tested with Codex CLI 0.159.0.

Every RemCTL app, whether downloaded or built yourself, carries the plugin inside it. Add it from there:

```bash
codex plugin marketplace add "$HOME/Applications/RemCTL Capability Host.app/Contents/Resources"
codex plugin add remctl@remctl-local
```

Start a new conversation and open 'Reminders' in the sidebar, or ask Codex to "Open my Reminders workspace." The first time, the plugin checks that RemCTL is ready and asks which list new reminders should go to.

Some features use Apple's private ReminderKit framework: sections, tags, attachments, templates, groups, smart lists, and more. Turn on 'Advanced Reminders features' in RemCTL's settings to use them. The workspace follows your Mac's light or dark appearance unless you pick one in the same settings.

If you connected Codex to RemCTL before (with `remctl mcp install` or during onboarding), remove that connection so Codex doesn't see RemCTL twice. This doesn't affect the plugin or your other AI apps:

```bash
remctl mcp remove --client codex
```

**From a checkout.** If you already registered a checkout with `codex plugin marketplace add .`, it keeps working. The plugin then follows your checkout instead of the installed app, so run the installer after every `git pull` to keep the two in step. To switch an existing checkout registration to the installed app, remove the old marketplace first:

```bash
codex plugin marketplace remove remctl-local
codex plugin marketplace add "$HOME/Applications/RemCTL Capability Host.app/Contents/Resources"
codex plugin add remctl@remctl-local
```

This changes where Codex finds the plugin; it does not remove RemCTL or your reminders.

## Update

Update RemCTL as usual (see [Upgrading](installation.md#upgrading)). The installer updates the CLI, host, and workspace together. Then refresh the plugin:

```bash
codex plugin add remctl@remctl-local
```

Codex can keep running the old version in an open workspace or conversation. Close and reopen the workspace, start a new conversation, and restart Codex if the sidebar entry still shows the old build. If an open workspace is still stuck, open Plugins → Manage in Codex, turn RemCTL off and on again, and reopen 'Reminders'. This doesn't touch your macOS permissions, so don't reinstall the host or reset permissions to fix a stale view.

## Custom install locations

The plugin starts `~/bin/remctl mcp`. If you installed RemCTL somewhere else, register a checkout instead of the app, and change the path in `plugins/remctl/mcp.json` in that checkout. Don't edit the copy inside the app; that breaks its signature. Keep your change when you pull updates.

## Remove

```bash
codex plugin remove remctl@remctl-local
codex plugin marketplace remove remctl-local
```

This removes the plugin only. RemCTL and your reminders stay. To remove RemCTL too, see [Uninstall](installation.md#uninstall).

## The workspace

The standard reminder tools used in conversation return data without opening a workspace. Open Reminders from the sidebar, or explicitly ask to open the workspace, when you want the interface. The standalone MCP server still offers its reminders widget to compatible clients.

**Lists.** The sidebar works like Reminders'. Today, Scheduled, Flagged, All, Completed, Assigned to Me, and Recently Deleted sit alongside your lists, groups, and custom smart lists, each with its real color, emoji, or Reminders symbol. Groups fold with their arrow. Hover over a list to pin it. Pinned lists become tiles at the top, in the same order as Apple Reminders, and pins sync back to Reminders.

**Layouts.** Switch between list, columns, and calendar from the toolbar. Each list, smart list, and view remembers its own layout; the 'Default layout' setting covers the rest. Lists show sections and nested subtasks. In columns, drop reminders into sections. In the calendar, drag a reminder to another day (it keeps its time), or double-click a day to create one. A 'No date' strip keeps unscheduled reminders in view.

**Rows.** Notes, saved link cards, and image attachments show inline, and a reminder with subtasks folds them with its 'N subtasks' button. Today splits into overdue, all-day, morning, afternoon, and evening. 'Load missing link previews' can fetch artwork from public websites when a link has none saved.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/readme/inspector-dark.jpg">
  <img alt="The inspector open beside a demo list" src="../assets/readme/inspector-light.jpg">
</picture>

**The inspector.** Click a reminder to edit its title, notes, list, date and time, repeat rule, priority, flag, URL, tags, section, assignment, Early Reminder, and location. It shows subtasks and attachments; click an image to preview it or save it to Downloads. Drop an image (PNG, JPEG, WebP, or HEIC, up to 8 MB) or a web link onto a reminder to attach it. Unsaved edits stay with each reminder while the workspace is open. Save with ⌘S, or click 'Cancel' to discard them.

**Quick add.** Click 'New Reminder' or press N for a floating panel above the Codex composer. Pick a list, date, time, priority, or flag, and press Return to add. ⌘Return adds it and keeps the panel open for the next one. Press Escape and your draft is still there next time.

**Right-click and drag.** Right-click a reminder, section, or list for its actions. Drag rows to reorder them, onto another list to move them, or drag a list onto a group. Select several reminders to act on all of them at once.

**Command palette.** ⌘K searches actions, lists, and reminders. With advanced features on, it also has forms for groups, sections, subtasks, manual ordering, Groceries, templates, smart lists, and attachments. The smart-list editor matches on tags (including exclusions), priorities, flags, absolute and relative dates, times, lists, and locations, and previews matches before you save. [Capabilities](desktop-capabilities.md) maps every RemCTL command to its place in the workspace.

## Keyboard

| Shortcut | Action |
| --- | --- |
| ⌘K | Search actions, lists, and reminders |
| ⌘F | Search reminders (the field in the toolbar) |
| N or ⌘N | New reminder |
| ⌘Return (while adding) | Add and keep the panel open |
| ⌘⇧N | New list |
| ↑ / ↓ | Move the selection |
| ⇧↑ / ⇧↓ or Shift-click | Extend the selection |
| ⌘-click | Add or remove one reminder from the selection |
| ⌘A | Select all loaded reminders |
| Return | Open the selected reminder |
| Space | Complete or reopen |
| ⌘S | Save the inspector |
| ⇧F10 | Open the selected reminder's menu |
| Delete | Delete (after a confirmation) |
| ⌘Z | Undo the last flag change, or completion of a non-repeating reminder |
| Escape | Close the frontmost menu, dialog, or inspector |

Shortcuts work while the workspace has keyboard focus. Text fields keep their normal shortcuts.

## Conversations

The workspace only shares what you choose. Select reminders and attach them to the conversation; each one shows up as a chip you can remove. 'Selected Reminders' keeps track of what's attached, and 'Choose reminders' lets you browse for more. You can also type `@RemCTL` in the composer to mention a single reminder.

'Ask ChatGPT…' drafts a prompt about the selected reminders and lets you review it and pick the current or a new conversation before sending. Nothing is sent without a click. 'Copy link' copies a link to the reminder in the workspace, and 'Open in Reminders' opens it in Apple's app.

## Export and import

Export saves the current view as a `.remctl` file, JSON, or CSV in Downloads. A `.remctl` file opens in a RemCTL viewer in Codex. (If Codex shows a text editor instead, choose Open options → RemCTL File.) From there you can edit the file and review an import.

Import creates new reminders (up to 1,000) and tells you which fields it can't bring over: original ids, completion state, private metadata, and attachments. It's a copy, not a backup. To bring back deleted reminders with their ids and subtasks, use Recently Deleted instead.

## Try it

Make a test list so your real reminders stay untouched, add a few reminders with dates, tags, and a subtask, and turn on 'Advanced Reminders features'. Then:

1. Press N, add a reminder, and press ⌘Return to add another. Press Escape mid-draft, reopen the panel, and your draft is still there.
2. Press ⌘K, jump to your test list, and use ↑, ↓, and ⇧↓ to select two reminders.
3. Switch to columns and drag both into another section. Switch to calendar and drag a timed reminder to another day.
4. Open a reminder, add a tag, change its repeat rule, and save with ⌘S. Check the result in Apple Reminders.
5. Build a smart list for the next seven days with one of your tags, preview it, and save it.
6. Attach two reminders to the conversation and ask Codex to plan your afternoon.
7. Delete a test reminder, then restore it from Recently Deleted.

## Limits

- Everything that uses private ReminderKit depends on how the current macOS version behaves.
- Smart lists with car rules can be saved, but the workspace can't preview them yet. It shows an error instead of wrong results.
- Assignment only works on shared lists, with the members Reminders reports.
- You can download any attachment, but you can only add images and links.
- Smart lists can't be renamed; the RemCTL CLI can't rename them either.
- The plugin also implements MCP Events (notifications when reminders change), but it's turned off in the workspace, because the Codex versions we tested don't offer event subscriptions. [Development notes](notes/events-2026-09-30.md) have the details.

## For maintainers

The plugin is made of a local marketplace (`.agents/plugins/marketplace.json`), the plugin manifest, skills, and icons (`plugins/remctl/`), the built workspace (`remctl_workspace.html`), and its source (`ui/`). Users don't need Node; the built HTML is checked in, and the installer copies all of it into the app.

After changing the interface or the plugin's Python files, rebuild the workspace and its connection fingerprint, then run the tests:

```bash
npm ci --prefix ui
npm run check --prefix ui
npm test --prefix ui
npm run build --prefix ui
python3 -m unittest discover -s tests -p 'test_desktop_plugin.py'
python3 -m unittest discover -s tests -p 'test_mcp_server.py'
python3 -m unittest discover -s tests -p 'test_events.py'
```

For a release, bump the version in `plugins/remctl/plugin.json` and commit the rebuilt `remctl_workspace.html` and `plugins/remctl/mcp.json` together. The icon is shared by the plugin, the MCP server, and the Capability Host; [its notes](notes/icon-provenance.md) have the source files.
