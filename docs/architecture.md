# Architecture

RemCTL separates the part that talks to you (the client) from the part that holds macOS permissions (the Capability Host). The client sends data commands to the host; setup commands run locally.

## Components

```text
AI app (Claude Code, Claude Desktop, Cowork, Codex, other MCP clients)
  └─ remctl mcp                      MCP server: stdio, or HTTP behind Tailscale
       └─ remctl <command> --json    one subprocess per tool call

Terminal, scripts, agents
  └─ remctl                         the client (Python 3.10+)
       ├─ local commands           completion, doctor, list-symbols, mcp, onboard, permissions, setup
       └─ data commands         sent over an owner-only Unix socket (protocol 2)

RemCTL Capability Host.app            signed, persistent, started by a LaunchAgent
  ├─ holds Full Disk Access, Reminders, and Automation
  └─ runs the same CLI from a sealed archive on a protected Python 3.13+
       ├─ reads the Reminders SQLite database
       ├─ remctl-bridge (Swift)       EventKit writes, address lookup
       ├─ remctl-private (ObjC)       ReminderKit writes, --private only
       └─ AppleScript                 flags

remctl-permissions (Swift)            the Full Disk Access helper window
```

Default paths:

```text
~/bin/remctl                                          the client and its modules
~/Applications/RemCTL Capability Host.app             the host
~/Library/LaunchAgents/net.macstories.remctl.capability-host.plist
~/Library/Application Support/RemCTL/capability-host.sock
~/.config/remctl/                                     onboarding state, MCP endpoint config
```

## Command routing

The client parses the command line first. Seven commands run in the client because they only touch local files or the terminal. Every other command carries data and runs in the host. `remctl_capability_policy.py` checks at startup that every parser command is in exactly one of the two sets.

`REMCTL_CAPABILITY_HOST` chooses the route:

- `auto` (default): use the host when it is installed and answers; otherwise run in the client.
- `force`: require the host; fail if it is unavailable.
- `direct`: run in the client. The client then needs its own permissions. `REMCTL_STORE_DIR` implies `direct`.

A hosted call sends the argument list, file descriptors for input files, and terminal state to the host over the socket. The socket directory is mode 0700 and the socket is mode 0600, owned by the user. There is no network listener and no token; the operating system checks the peer's user id. Confirmation prompts, signals, and cancellation cross the boundary, so `remctl delete` on a terminal still asks first.

The host verifies its own signature and runtime before running anything. It executes the CLI from an archive sealed into the app binary, with the bridge, the private helper, the store path, and scratch paths fixed by the installed generation. Caller-side helper overrides are ignored on the hosted route.

## The Capability Host

The host is an AppKit app signed with an Apple Development identity. macOS ties privacy grants to that signature, so the installer preserves the identity across upgrades and refuses to publish a build whose Team ID or designated requirement would change. This is why RemCTL does not use ad-hoc signing.

The LaunchAgent keeps the host running and restarts it after login. `install.sh` publishes the app, the sealed archive, the LaunchAgent, and the socket as one transaction: it stages and verifies a complete generation, stops the old service, swaps the files, starts the new service, and only then commits. On failure it restores the previous generation. A hash manifest records every installed file so upgrades and the uninstaller refuse to touch files RemCTL does not own.

## Reads

Reads open the iCloud Reminders store read-only:

```text
~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/Data-*.sqlite
```

Database reads include fields EventKit does not expose: sections, subtasks, tags, attachments, deep links, list colors and icons, recurrence details, urgent state, Early Reminders, and manual ordering. RemCTL never writes to the database.

List reads batch-load subtask counts, tags, and badge indicators in `remctl_serialization.py` and record explicit zeros so no per-reminder query follows.

Attachments: each attachment row stores a filename, a UTI, pixel dimensions, and a SHA-512 of the file. The file lives outside the database:

```text
~/Library/Group Containers/group.com.apple.reminders/Container_v1/Files/Account-*/Attachments/<sha512><ext>
```

`remctl_images.py` finds the file by trying candidate extensions and accepts it only when the hash matches. That verified path is `attachments[].path` in JSON. Rows without a hash are legacy attachments that were never downloaded; they serialize as `path: null`, `resolved: false`. Inline rendering (`--images`) is a display feature of the read side: Kitty graphics, iTerm2's protocol, or half-block characters, decided from the terminal, with Pillow when available and macOS `sips` otherwise.

### Limited reads through EventKit

`--via-eventkit` on `show`, `search`, `today`, and `upcoming` reads through `remctl-bridge` with public EventKit predicates. It does not change the route: on `auto` the host performs it. The result is a wrapper with `source`, `fidelity: "limited"`, `idWarning`, and `items` whose ids are EventKit identifiers. It has no sections, tags, private metadata, or table output. It exists for recovery when Full Disk Access is not yet granted.

## Writes

1. **EventKit through `remctl-bridge`.** The normal path for create, edit, list moves, complete, delete, recurrence, alarms, location alarms, notes URLs, and list management. The client validates input first; the bridge validates again and returns structured errors so the client can tell a permission denial from a move rejection.
2. **AppleScript.** Sets flags for `flag`, `unflag`, and ordinary `add --flag`; EventKit has no flag property. Private flag writes use ReminderKit. Also a fallback for a few operations after a recovery check that prevents duplicates. A failed flag write returns an error.
3. **ReminderKit through `remctl-private`.** Used only with `--private`, plus one automatic case: when EventKit rejects a pure list move (parents with subtasks, shared-list boundaries), RemCTL clones the reminder into the destination with ReminderKit, verifies the clone and its subtasks, and deletes the original. It is not used for permission errors, timeouts, or moves combined with other edits.

`remctl-private` reads one bounded JSON request on stdin, performs one of a fixed set of actions, and saves through the Reminders stack. It never runs a shell, never accepts arbitrary selectors, and never writes the database. It answers a `protocol_version` handshake (currently 2); the client refuses an older helper. Paths that once failed silently now return explicit errors, and the account lookup accepts only CloudKit accounts.

**Address lookup.** `location-lookup` and `--location-address` ask `remctl-bridge` to geocode an address with CoreLocation's public geocoder. The bridge handles that action before it opens EventKit, so it needs no Reminders or Location Services permission, and it cancels the request after a deadline (10 seconds by default). It returns every match with its coordinates, address parts, and region size. The client then decides: it uses a match only when there is exactly one, its region is under about a kilometer, it names the street or place in the query, and the query also gives a town or postal code. Otherwise it stops before any write. The last two checks exist because Apple's geocoder returns one best guess even for a street it did not find. Address lookup contacts Apple; rich-link validation and Reminders synchronization may also use the network.

Private rich URLs must resolve to public `http` or `https` hosts. Loopback, `.local`, private, link-local, multicast, reserved, and unresolved hosts are rejected before writing.

## The MCP server

`remctl_mcp.py` is a Model Context Protocol server with no dependencies beyond the standard library. `remctl mcp` runs it over stdio; `remctl mcp serve --http` runs it as a Streamable HTTP endpoint on the loopback interface.

Both transports use the same handler: `MCPServer.handle_message` takes one JSON-RPC message and returns the response. It implements the 2026-07-28 revision (per-request `_meta`, `server/discover`, `resultType`, cache hints) and the `initialize` handshake of 2025-11-25 through 2024-11-05. A request that carries the modern `_meta` fields is served statelessly; an `initialize` selects legacy semantics for that process (stdio) or that `Mcp-Session-Id` (HTTP).

Every tool builds an argument list for the CLI and spawns `<python> <remctl> <args> --json` with `REMCTL_SKIP_ONBOARD=1`. Standard output becomes `structuredContent` (arrays wrapped as `{"items", "count"}`); a nonzero exit becomes `isError: true` with RemCTL's structured stderr error when it emitted one. Calls run concurrently on a small thread pool; `notifications/cancelled` terminates the subprocess. Because the CLI is the only path, the host and its permissions are unchanged.

Cancellation is scoped to the stdio connection or the legacy HTTP session, including stdio calls waiting for a worker. Stateless HTTP requests have separate request-ID namespaces; a cancellation notification cannot cancel a different stateless request by guessing its ID. Partial writes retain their structured result, including created IDs, even when the command exits with an error.

The MCP Apps widget is one HTML file, `remctl_mcp_widget.html`, served as the resource `ui://remctl/reminders-v1.html`. The server attaches the widget to tools and results only for clients that negotiate the `io.modelcontextprotocol/ui` extension, and adds result hints under `_meta["net.macstories.remctl/ui"]` that tell the widget which view to render and which tools its buttons call.

The HTTP endpoint requires `Authorization: Bearer <token>`, validates `Origin` and `Host` against loopback and the Mac's Tailscale identity, mirrors the modern headers (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`) against the body, creates `Mcp-Session-Id` values for legacy clients, and answers `GET /health` without a token. `remctl mcp install --client tailscale` stores the token in `~/.config/remctl/mcp-http.json`, installs the `net.macstories.remctl.mcp-http` LaunchAgent, and runs `tailscale serve` so Tailscale provides HTTPS and the network boundary. [mcp.md](mcp.md) documents the tools and the setup.

HTTP checks authentication before reading a body and closes rejected connections. It accepts one nonnegative `Content-Length`, up to 4 MiB, and does not accept transfer encoding. It allows 32 open connections and 256 legacy sessions; sessions expire after an hour without activity. An expired session gets HTTP 404 and must initialize again. Installation reloads an already-loaded HTTP service after publishing and checks its new process and health response.

Installed HTTP endpoints read the current token file for each request. Token rotation therefore revokes old credentials even when the endpoint was started manually. A missing or malformed token file denies access. Rotation does not undo a tool call that was already authenticated and started.

## Data model notes

**List appearance** lives on `ZREMCDBASELIST`: `ZCOLOR` (an archived `REMColor`), `ZBADGEEMBLEM` (an emoji as JSON or an emblem name such as `education3`), `ZISPINNEDBYCURRENTUSER` and `ZPINNEDDATE` for pins, `ZISGROUP` and `ZPARENTLIST` for groups, and the `ZSHOULDCATEGORIZEGROCERYITEMS`, `ZSHOULDAUTOCATEGORIZEITEMS`, and `ZGROCERYLOCALEID` columns for Groceries lists. Private writes use `REMSaveRequest.updateList`, `REMListChangeItem.setColor`, the appearance context's `setBadgeEmblem` and `setBadge`, `setIsPinned`, and the grocery context. `list-symbols` lists the 71 emblem names bundled in RemindersUICore; `--symbol` accepts only those because Reminders draws unknown names as the default icon.

**Groups** are list rows with `ZISGROUP`. `group-create` uses `REMSaveRequest.addGroupWithName`; membership changes set `REMListChangeItem.parentSubContainerID`; `group-delete` detaches children before removing the group. Reordering children detaches and reattaches them in reverse order because assigning a list to a group places it at the top.

**Groceries** sorting: after `add --private --grocery`, RemCTL polls section membership because Reminders sorts new items itself, and uses the private categorizer only for unsorted items. The categorizer differs by macOS release: Tahoe uses `categorizeGroceryItemsWithReminderIDs:` on the grocery context with UUIDs; Golden Gate uses `autoCategorizeRemindersWithReminderIDs:` on the list change with `REMObjectID` values. The helper picks the path by capability, never by name alone.

**Smart lists** are `REMCDSmartList` rows (`Z_ENT = 4`) with `ZSMARTLISTTYPE` and `ZFILTERDATA`, which on macOS 26 is UTF-8 JSON. `smart-list-create` resolves the CloudKit account, verifies `supportsCustomSmartLists`, calls `REMSaveRequest.addCustomSmartListWithName`, attaches the change item to the account, sets the supported-version fields to `20220430` (required, or Reminders shows zero filters), and sets the filter and appearance. The write path allows only filter shapes verified to appear in Reminders. Pinning a smart list updates `ZPINNEDDATE`; RemCTL reports a positive date as pinned. Built-in smart-list pinning depends on a generic fetch that Golden Gate no longer exposes, so it is capability-gated.

**Manual ordering** is stored as ordering JSON on ordinary lists and `REMCDManualSortHint_v1` rows. `reminder-move` inserts through `REMListChangeItem.insertReminderChangeItem:before/afterReminderChangeItem:` for lists and rewrites `REMManualOrdering` through `REMSmartListChangeItem.updateManualOrdering:` for unsectioned custom smart lists. `show` applies the stored order to its rows.

**Templates** live in `ZREMCDTEMPLATE` (templates), `ZREMCDSAVEDREMINDER` (saved reminders, with a prefixed JSON `ZMETADATA`), and `ZREMCDBASESECTION` rows pointing at a template. Writes use `REMSaveRequest.addTemplateWithName:configuration:toAccountChangeItem:`, `addListUsingTemplate:`, and `updateTemplate` for deletion. Public template links are read but never created, because the private sharing call can report success without producing a link.

**Recurrence** is written through EventKit and read back from `ZREMCDOBJECT` rows. JSON reports `frequency`, `interval`, `daysOfWeek`, and `daysOfWeekDetailed` with `weekNumber` (0 any week, 4 the fourth, -1 the last). The bridge validates week numbers before constructing `EKRecurrenceDayOfWeek`, which otherwise raises an uncatchable exception.

**Due dates and alarms.** `dueDate` comes from `ZDUEDATE`; `displayDate` from `ZDISPLAYDATEDATE` when present. Reminders.app lists and labels a reminder at `ZDISPLAYDATEDATE`, which follows an absolute alarm set to another time than the due date, so `today`, `overdue`, `upcoming`, and `stats` bucket by it and fall back to `ZDUEDATE`. Date-only input creates all-day reminders through date components. On `edit -d`, absolute alarms equal to the old due time move with it; `edit -d clear` removes them. Reminders can store the same alarm more than once, one acknowledged copy for each device that handled the notification. Both rules act only when every alarm matches, however many copies there are. Alarms are `ZREMCDOBJECT` rows (`Z_ENT = 15`) with triggers; relative, absolute, and location triggers serialize under `alarms`. Writing a location alarm replaces the previous one.

**Flags, urgent, Early Reminders.** Flags come from `ZFLAGGED`; non-private flag writes use AppleScript addressed by reminder id so lists inside groups work; `remctl-bridge` refuses flag payloads. Urgent comes from `ZISURGENTSTATEENABLEDFORCURRENTUSER` and is written only with `--private`. Early Reminders live in `ZREMCDDUEDATEDELTAALERT` with a JSON mirror on the reminder (`dueDateDeltaUnit` 0 and `dueDateDeltaCount` -15 is "15 minutes before") and are written through `REMReminderChangeItem.dueDateDeltaAlertContext`, replacing existing alerts first.

**Shared-list assignment** resolves the sharee from local `REMCDSharee` rows and writes through ReminderKit's assignment context with the current user as originator.

## Output safety

Human output strips terminal control characters from titles, notes, URLs, list names, section names, and tags. JSON keeps the raw values.

## Permissions

The host is the only macOS privacy target: Full Disk Access for the database, Reminders for EventKit, Automation for AppleScript. Callers reach those grants through the socket and need none of their own. Direct mode is the exception: it runs in the caller, so a denied direct check is expected while the effective route is ready. `remctl onboard` requests the Reminders and Automation grants only when they are missing and guides the Full Disk Access step; `remctl doctor` reports the state.

## Private API compatibility contract

`remctl-private` has a read-only `capabilities` action for drift checks. It reports the host OS, the grocery categorization selectors, the generic and custom smart-list fetch selectors, normalized Objective-C encodings when available, and `saveCalled: false`. The grocery check creates an in-memory change object but never saves. Production dispatch is capability-based, as described above. The helper links `ReminderKit`, Foundation, and AppKit only. `scripts/live_private_matrix.py` runs a disposable write, read-back, and cleanup matrix against a live store; see [private-api-audit-2026-08-12.md](private-api-audit-2026-08-12.md) and [macos27-compat-review.md](macos27-compat-review.md) for the audits.

## Environment overrides

```bash
REMCTL_CAPABILITY_HOST=auto|force|direct
REMCTL_STORE_DIR=/path/to/reminders/store     # implies direct
REMCTL_BRIDGE_PATH=/path/to/remctl-bridge     # direct only
REMCTL_PRIVATE_PATH=/path/to/remctl-private   # direct only
REMCTL_PERMISSIONS_PATH=/path/to/remctl-permissions
REMCTL_CONFIG_DIR=/path/to/config
REMCTL_SKIP_ONBOARD=1
REMCTL_IMAGES=1
REMCTL_IMAGE_MODE=kitty|iterm2|halfblock|none
REMCTL_IMAGE_WIDTH=32
REMCTL_MCP_DEBUG=1
NO_COLOR=1
```

Source-tree helper testing uses direct mode with explicit helper paths:

```bash
REMCTL_CAPABILITY_HOST=direct REMCTL_BRIDGE_PATH=./remctl-bridge remctl add "Test"
REMCTL_CAPABILITY_HOST=direct REMCTL_PRIVATE_PATH=./remctl-private remctl edit 123 --private --url https://example.com
```
