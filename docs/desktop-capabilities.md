# Workspace capabilities

This maps every RemCTL command to where it lives in the [Codex workspace](desktop-plugin.md). A "form" is a validated form in the ⌘K command palette. Forms exist even for things that also have a direct control. Output flags such as `--format` map to the workspace's views and export formats instead.

| RemCTL commands | In the workspace |
| --- | --- |
| `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `show`, `search` | Sidebar, search, calendar, and command palette. Urgent and Overdue have their own palette entries. |
| `info`, `add`, `edit`, `done`, `undone`, `flag`, `unflag`, `delete` | Inspector, new reminder panel, row controls, right-click menus, and multiple selection. |
| `subtasks`, `reminder-move` | Nested subtasks, subtask creation in the inspector, moves between parents, sections, and lists, and ordering forms. |
| `tags` | Inspector tag chips with suggestions, clickable tags on rows, search, and smart-list filters. |
| `lists`, `list-info`, `list-create`, `list-edit`, `list-rename`, `list-delete`, `list-pin`, `list-unpin` | Sidebar, pin buttons, the appearance editor, list menus, and forms. |
| `list-symbols` | Your Mac's own Reminders symbols (71 of them), plus emoji and colors, in the appearance picker. |
| `groups`, `group-info`, `group-create`, `group-edit`, `group-delete` | Grouped sidebar, dragging lists into groups, group menus, and forms. |
| `sections`, `section-create`, `section-rename`, `section-delete` | Section headings, columns, section menus, drag and drop, and forms. 'Move to Section › None', dropping a card in the Others column, and clearing the inspector's section take a reminder out of its section (`edit --no-section`). |
| Smart-list sections: `section-create --smart-list`, `edit --smart-list` with `--section`, `--new-section`, or `--no-section`, `show --smart-list` | The Section Create and Update Reminder forms take a smart list. The workspace doesn't draw a smart list's sections as columns yet; `show --smart-list` and `get_reminder`'s `smartListSections` read them. |
| `sharees` | The assignment picker, filled with the shared list's real members. |
| `location-lookup` | Location search, plus address, coordinates, radius, and arriving/leaving in the inspector. |
| `smart-lists`, `smart-list-create`, `smart-list-edit`, `smart-list-delete` | Sidebar and a visual editor with preview: flags, priorities (including No Priority), tags (including exclusions and untagged), absolute, relative, and no-date rules, times, lists, locations, and car rules. The raw filter JSON is still editable. |
| `templates`, `template-info`, `template-create`, `template-apply`, `template-delete` | Template browser, list menus, and forms. |
| Groceries options on `add`, `edit`, `list-create`, `list-edit` | List type and language controls, item categories, and forms. |
| `--image` and rich links on `add` and `edit` | Drag and drop, the file picker, the inspector gallery and preview, and saving attachments to Downloads (up to 50 MB). New images can be PNG, JPEG, WebP, or HEIC, up to 8 MB each. The inspector's URL field adds a link; the Update Reminder form's `set_url` and `clear_urls` replace or remove them. |
| `--messaging-contact`, `--clear-messaging-contact` | The Create Reminder and Update Reminder forms. A person's name needs Contacts access, which you allow once with `remctl permissions contacts` in Terminal. |
| `deleted`, `restore` | Recently Deleted, which restores reminders with their original ids into a list you choose. |
| `link`, `open` | 'Copy link', 'Open in Reminders', and link forms. |
| `stats` | 'Reminder Statistics' in the command palette. |
| `export`, `import` | `.remctl`, JSON, and CSV export, plus a `.remctl` viewer and editor with a reviewed import. |
| `doctor`, `onboard`, `setup`, `permissions` | Diagnostics in settings and the plugin's first-run check. macOS permissions still go through `remctl onboard`, and optional Contacts access through `remctl permissions contacts`. |
| `completion`, `mcp`, `workspace` | Not in the workspace. These set up shells, AI apps, and the plugin itself. |

Repeat rules, Early Reminders, alarms, urgent state, locations, assignment, and clearing fields are also in the full Reminder Fields form. The command palette, right-click menus, keyboard navigation, multiple selection, drag and drop, conversation context, and file viewer are workspace extras with no CLI equivalent.

## Limits

- Smart lists with car rules can be saved, but previews and reads return an error because Reminders doesn't expose the car metadata RemCTL would need.
- Assignment needs a shared list with real members. It wasn't tested on a shared list with other people.
- Any existing attachment can be downloaded. New attachments are limited to images and links.
- Smart lists can't be renamed, because the CLI can't rename them. You can change their filter, color, emoji, and symbol.
- `.remctl` import creates new copies of the supported fields. It isn't a backup restore, and it lists the fields it leaves out before you import.
- Private ReminderKit features depend on your macOS version and need 'Advanced Reminders features' turned on.
- MCP Events is built but turned off. See the [Events notes](notes/events-2026-09-30.md).
