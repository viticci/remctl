# Move reminders between lists

Use the MCP `move_to_list` tool with `reminder_id`, either `list_id` or `list`, and `confirm: true`. It can copy the reminder and delete the source, and a shared destination makes the copied contents visible to its participants. The tool advertises `destructiveHint: true` and is not idempotent.

`update_reminder` keeps its list parameters so existing clients receive a typed `move_required` error with instructions to use `move_to_list`. Move first, then apply other edits to the returned ID. The command-line equivalent is `remctl edit ID --list-id LIST --json`; it rejects combined edits when a copy is required.

Shared destinations and parents with subtasks use ReminderKit's existing copy operation. An ordinary in-place move also falls back to copying when the bridge reports ReminderKit error `-3002` or a recognized container rejection. Authorization failures, timeouts and execution failures don't trigger copying.

The source is removed only after the copy is read back from the destination. Verification checks title, notes, due and display dates, time zone, completion state and date, priority, flags, all rich-link URLs, and each copied subtask's identity, parent, destination and fields. Copies get new identities and may get new creation dates. If the source's subtasks can't be read, the move stops before writing. If the copy doesn't match, `list_move_copy_unverified` identifies the retained source and the new copy; inspect both before retrying.

For a successful copy, use the returned `id` for subsequent operations. `oldId`, `method: "clone-delete"`, `subtasksMoved` and `originalDeleted` describe the move. Native preservation and shared-list behavior still require a disposable shared-list fixture on the receiving Mac; unit tests don't establish that live result.
