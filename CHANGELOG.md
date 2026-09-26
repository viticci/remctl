# Changelog

## 2.0.0 — Unreleased

### Recently Deleted

- `deleted` and the `recently_deleted` MCP tool show Apple's recoverable reminders with numeric IDs, pagination, and nested subtasks. `info --include-deleted` and `get_reminder` with `include_deleted: true` can inspect them.
- `restore` and `restore_reminder` recover a parent and its subtasks into an explicit list in the same account. Recovery requires private API opt-in and verifies the original IDs and hierarchy.
- Delete help explains the recovery window. Deleted reminders have no edit or delete controls in the MCP widget.

### Images, rich links, and recurrence imports

- `add` and `edit` accept `--image` without confusing it with global image display options.
- Private rich links accept hostname DNS results in the fake-IP range used by TUN proxies. Literal fake-IP targets and other private DNS results remain blocked.
- JSON import and subtask JSON accept exported recurrence objects, including weekday positions, date filters, occurrence counts, and end dates. Invalid rules fail validation before writing.

### Installation fixes

- Custom-prefix installs place the LaunchAgent in `~/Library/LaunchAgents` so it starts at login. Upgrades migrate the old prefix-based plist after checking ownership, and restore it if publication fails.
- Protected Python errors identify the failing path, owner, mode, and condition. Installation docs explain the group-write repair needed by some python.org installs.
- The host uses the spawn working-directory function declared by older macOS SDKs.
- `doctor` distinguishes an unloaded signed host from missing permissions and gives its startup command.

### Notes, list colors, and live tests

- `edit ID --url URL` preserves existing notes. An explicit `--notes` value replaces them before appending the URL. The AppleScript fallback also supports clearing notes with `--notes ""`.
- `list-create --color gray` and `--color teal` now set the requested color. Color names ignore surrounding whitespace and case. The EventKit helper rejects unknown colors before saving.
- The private live matrix now skips standalone helper writes unless `--standalone-helper` is supplied. Those writes bypass the signed host and previously failed in a normal installation where only the host has Reminders access. Hosted CLI checks still run.
- Simplified the documentation and corrected stale tool counts, edit behavior, MCP routing guidance, and location-alarm details.

### Search, batches, locations, and typed metadata

Closes the gaps found by comparing RemCTL with `remindctl`, the Reminders CLI behind Hermes Agent's bundled skill ([docs/remindctl-comparison-2026-09-26.md](docs/remindctl-comparison-2026-09-26.md)).

- `search` matches saved rich links as well as titles and notes, ignores case and accents (`cafe` finds `Café`), and keeps `%`, `_`, and `\` literal. `--list` and `--list-id` limit it to one list; duplicate list names stop with the candidate ids. It no longer drops matches past 100 without saying so: `--limit` and `--offset` page through every match with `total`, `hasMore`, and `nextOffset`, and plain `--json` warns on stderr when more matches exist. The `--via-eventkit` search also matches the reminder's URL.
- `done`, `undone`, and `delete` take up to 50 ids. Every id is looked up before the first write, repeated ids are written once, and `--json` reports each id with `succeeded`, `failed`, and `uncertain` lists. `delete` confirms the whole batch once and still needs `--force` with `--json`.
- `done` no longer risks completing a repeating reminder twice. When the bridge timed out or crashed, it retried through AppleScript, which could advance the series by two occurrences. It now retries only when the bridge reported that it saved nothing, and otherwise reports `completion_uncertain`. A batch stops after an uncertain or stalled write, starts no write after 60 seconds, and reports the ids it did not reach as skipped.
- `--location-address` on `add` and `edit` turns a street address into a location alarm. The signed host looks it up with Apple's geocoder before anything is written, and RemCTL uses the match only when it is the only one, is street-sized, names the street or place typed, and the address gives a town or postal code. Apple's geocoder returns a best guess even for a street it did not find, so anything else stops the command, as do a personal label such as `Home` and a timeout. `remctl location-lookup` shows the match without writing. `add` now accepts every location option `edit` had. Radius must be 1 to 100000 meters, and non-finite coordinates are refused.
- `remctl list-info` returns one list with its section ids and sharees, which section and assignment writes need. `list-create --json` reports the new list's numeric `id`.
- MCP: `search` gained `list`, `list_id`, `limit`, and `offset` and always returns a page; `set_completion` and `delete_reminder` take `reminder_ids`, get 300 seconds, and `set_completion` is no longer marked idempotent; `create_reminder` and `update_reminder` take `private: true` plus synced tags, rich links, sections, subtasks, assignment, Early Reminders, urgent state, and coordinate or address location alarms. Without the opt-in those fields are refused and the plain tag and URL fallbacks are unchanged. New tools: `get_list`, `resolve_location` (the only tool marked `openWorldHint`), `create_list`, and `update_list`. Batch results survive a nonzero exit, and the widget shows them per id.
- `remctl mcp config --format hermes` prints the `mcp_servers` entry for Hermes Agent. [docs/hermes.md](docs/hermes.md) explains the setup and how RemCTL differs from `remindctl`.

### Runtime review fixes

- Reject unauthenticated HTTP requests before reading their bodies. Reject negative, duplicate, and unsupported body framing; bound open connections and legacy sessions. Malformed headers, cancellation IDs, and Unicode no longer crash request handling.
- Keep request IDs and cancellation within each client session. Reject duplicate active IDs and honor cancellation while a stdio command is still queued, before it can write.
- Pass empty notes through MCP so `update_reminder` can clear them. Preserve created IDs and retry details when imports or private writes partly succeed.
- Write private config files atomically. Use private, unique temporary files for client registration and preserve each backup, avoiding predictable temporary-path overwrites.
- Read the current token for each HTTP request so rotation also revokes credentials on manually started endpoints. Report failed service restarts and unhealthy Tailscale setup as failures. Upgrades reload and verify an already-loaded HTTP endpoint so it uses the new runtime.

### MCP server

- Added `remctl mcp`, a local MCP (Model Context Protocol) server over stdio with no third-party dependencies. It implements the stateless MCP 2026-07-28 revision (`server/discover`, per-request `_meta` protocol and capability fields, `resultType`, `serverInfo` and cache hints on every result, `-32022` version errors with the supported list) and supports `initialize`-based clients on 2025-11-25, 2025-06-18, 2025-03-26, and 2024-11-05. Verified against the official MCP Python SDK 2.2 client in modern and legacy modes.
- Nineteen tools with input schemas, output schemas, annotations, and structured results: `today`, `upcoming`, `overdue`, `flagged`, `search`, `show_list`, `lists`, `get_list`, `get_reminder`, `resolve_location`, `create_reminder`, `update_reminder`, `set_completion`, `set_flagged`, `delete_reminder`, `create_list`, `update_list`, `doctor`, and a `run` tool for other data commands. Two prompts: `daily_review` and `plan_week`. Every tool runs the installed CLI with `--json`, so the signed Capability Host keeps owning macOS permissions and clients need no grants. RemCTL's structured stderr errors are returned as tool errors the model can act on; `notifications/cancelled` terminates the tool's subprocess.
- MCP Apps widget (`ui://remctl/reminders-v1.html`, extension `io.modelcontextprotocol/ui`, spec 2026-01-26), adapted from MacRemote's shared widget: reminder rows with Reminders-style check-off, reschedule, rename, and two-step delete, section headers, single-reminder cards, change confirmations with warnings, list and doctor views, host theming, and capability-scoped linkage with legacy aliases.
- New onboarding step and commands for supported clients: `remctl onboard` now offers to connect detected AI apps; `remctl mcp install` uses `claude mcp add` for Claude Code, `codex mcp add` for Codex, and a backed-up merge into `claude_desktop_config.json` for Claude Desktop and Cowork; `remctl mcp bundle` builds a one-click `.mcpb` desktop extension; `remctl mcp status`, `config`, and `remove` complete the set. `doctor` gained an `mcp_clients` check. `mcp` joins the local command set (seven local commands; data commands run in the host).
- The installer publishes `remctl_mcp.py`, `remctl_mcp_widget.html`, and the MCP icons in the same ownership manifest; the uninstaller removes them.

### Other devices over Tailscale

- `remctl mcp serve --http` runs the same server as a Streamable HTTP endpoint on the loopback interface: bearer-token auth on every request, `Origin` and `Host` validation, the 2026-07-28 header mirroring rules (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` with `-32020` on mismatch), `Mcp-Session-Id` sessions for legacy clients, `GET /health`, and a keep-alive-safe reject path.
- `remctl mcp install --client tailscale` creates a private token (`~/.config/remctl/mcp-http.json`, mode 0600), installs the `net.macstories.remctl.mcp-http` LaunchAgent, and mounts the endpoint at `https://<mac>.<tailnet>.ts.net/remctl` with `tailscale serve`, so Claude Code, Codex, and other clients on the user's other tailnet devices can connect. `remctl mcp config --format tailscale` prints the commands per client; `remctl mcp token --rotate` rotates the token; `remctl mcp remove --client tailscale` tears it down. `doctor` reports the endpoint (`mcp_tailscale`).
- Verified with the official MCP Python SDK client over HTTPS in modern and legacy modes, and with `claude mcp add --transport http` and `codex mcp add --url`.

### Guided onboarding

- `remctl onboard` is now a step-by-step flow: macOS permissions, a health check that reads today's reminders, connecting each detected AI app with one question, and an optional Tailscale step that appears only when Tailscale is installed. It shows what is already done, asks before every change, and ends with the next steps. `--no-mcp` and `--no-tailscale` skip steps; `--json` reports without asking.
- Onboarding no longer re-requests permissions that are already authorized. Re-requesting an authorized grant through the host could block for minutes on a healthy Mac.

### Fixes

- `create_reminder` now reports the new reminder's numeric `id`, the id every other tool accepts. `remctl add --json` reports the CloudKit identifier as `id` and the number as `numericId`, so a model that passed the created `id` back to `get_reminder` was told `reminder_id must be an integer`. The CloudKit identifier is still reported, as `cloudKitId`. When RemCTL cannot read the number back, the result carries a `numeric_id_unavailable` warning rather than an id that cannot be used. The CLI's own `add --json` output is unchanged.
- `create_reminder` and `update_reminder` accept Apple's numeric priorities (`0`, `1`-`4`, `5`, `6`-`9`) as well as the names, and `tags` accepts a list of strings as well as a comma-separated string. Out-of-range numbers and unknown names are still refused. Without `private: true`, creation adds `#hashtags` to the title; synced tags require the private option.
- `doctor` no longer reports a starting Capability Host as a broken install. The host answers `unknown` until its first permission refresh lands, and `targetNotRunning` while macOS cannot reach Reminders. `doctor` graded both as failures, so `eventkit`, `automation`, `capability_host`, and `effective_access` failed and the command exited 1 while reads and writes worked normally. `doctor` now waits up to three seconds for the host to finish verifying, reports a permission it still cannot read as a warning, and keeps `effective_access` accurate. A refused or restricted grant is still a failure. Because the `doctor` MCP tool runs the CLI, a restarted host no longer makes that tool return an error.
- Connected AI apps and the tailnet service keep working after `brew upgrade`. `remctl mcp install` registered the Python it resolved to, which for Homebrew is a versioned folder such as `/opt/homebrew/Cellar/python@3.14/3.14.7/`. `brew upgrade` deletes that folder, and every app then failed to start the server. RemCTL now registers the formula's stable `opt` path, such as `/opt/homebrew/opt/python@3.14/bin/python3.14`. Run `remctl mcp install` once, and `remctl mcp install --client tailscale` if you use the tailnet endpoint, to rewrite existing entries.
- `doctor` and `remctl mcp status` judge an MCP connection by whether its Python still works, not by whether it is the Python that runs the check. Before, every app registered from another Python was reported as pointing at a different RemCTL path, and the verdict changed with the shell that ran `doctor`. A stale connection now names its reason: a different RemCTL path, a Python that no longer exists, or a versioned Homebrew Python. `doctor` also warns when the tailnet service starts such a Python.
- `doctor` no longer reports the tailnet endpoint as not serving when it runs without a terminal, for example as the `doctor` MCP tool from Claude on iPhone. The `tailscale` binary inside Tailscale.app acts as the command-line tool only when `TERM` is set; otherwise it tries to open the app and prints an error. RemCTL now sets `TAILSCALE_BE_CLI=1` for its Tailscale commands.
- `remctl mcp install --client tailscale` works when the tailnet service is already running. It failed with `Bootstrap failed: 5: Input/output error` and left the endpoint stopped, because `launchctl bootout` returns before launchd has stopped the old service. RemCTL now waits for the old service to exit before it starts the new one.
- Reinstalling the tailnet service no longer takes it offline for 10 seconds. RemCTL restarted the service right after `launchctl bootstrap` had started it, and launchd held that second start for its 10-second throttle. RemCTL now restarts the service only when the old one is still loaded.
- `remctl mcp status` no longer calls a connected Claude Code or Codex "not installed". Both install their CLIs in `~/.local/bin`, which GUI apps, LaunchAgents, and remote runners such as MacRemote often lack on PATH. Status now reads each app's own MCP config first, as `remctl doctor` does.
- `edit -d clear` and `update_reminder` with `due: clear` refuse a repeating reminder before they change anything, with the code `repeating_reminder_requires_due_date`. Reminders cannot save a repeating reminder without a due date. Before, the command waited up to 23 seconds, the AppleScript fallback applied the other changes in the same edit, such as a new title, and then failed with a message that did not say why. The AppleScript fallback now sets the date first, and when every write path fails, the error includes the bridge's own message.
- `add` and `edit` refuse a relative alarm (`15m`, `1h`, `1d`) on a reminder without a due date, with the code `relative_alarm_requires_due_date`. A relative alarm counts back from the due date, so Reminders saved it but it never fired. An alarm at a fixed date and time still works without a due date.
- `set_completion` refuses `completion_date` when `completed` is false. Before, it reopened the reminder and ignored the date without saying so.
- The AppleScript fallback for `done`, `undone`, `delete`, and `edit` finds reminders in lists that are inside a list group. It addressed the reminder through its list, which Reminders' AppleScript cannot see inside a group (error -1728). It now addresses the reminder by its id, as `flag` already did.
- MCP tools pass values that start with `-` as values. A search for `-urgent`, a new title such as `-Renamed`, or notes that begin with `--` made the command line read the value as an option and fail. The tools now use `--option=value` and `--`.
- The `run` tool finds the command behind top-level options. `run` with `["--format", "json", "mcp"]` started a second MCP server, because the guard took `json` for the command. It now skips top-level options and their values, including abbreviations such as `--form`.
- The reminders widget shows weekday names and the occurrence count for repeating reminders. RemCTL reports weekdays as EventKit numbers, where 1 is Sunday, and the count as `count`. The widget expected names and `occurrenceCount`, so it showed `weekly 2, 4` and left out the count.
- An all-day reminder's `dueDate` is midnight of its day, such as `2026-09-30T00:00:00`, in every time zone. Reminders stores the day as midnight UTC, and RemCTL printed that moment in local time: `2026-09-30T02:00:00` in Rome and `2026-09-29T20:00:00` in New York, the day before. `displayDate` is left out when it names the same moment.
- `today`, `overdue`, `upcoming`, and `stats` place a reminder on the day Reminders.app shows it. Reminders lists and labels a reminder at its display date, which follows an alarm at a fixed date and time. RemCTL used the due date for timed reminders, so a reminder whose due date had moved to Thursday while its alarm stayed on Tuesday was overdue in the app but missing from `today`. Human output and the MCP widget now label such a reminder at the app's time, and `info` prints both times. JSON keeps `dueDate` as the due date, with `displayDate` beside it.
- `edit -d` moves the alarm with the due date when Reminders stored that alarm more than once. Each device that handles a notification writes its own acknowledged copy of the alarm, and the copies sync. RemCTL moved the alarm only when a reminder had exactly one, so rescheduling such a reminder changed its due date but left the alarm, and the date Reminders shows, at the old time. `edit -d clear` had the same limit.
- The command guide's recurrence examples now include a due date. Reminders refuses to save a repeating reminder without one, so the examples failed as written.

### Documentation

- Rewrote the README, installation guide, command guide, MCP guide, architecture guide, and the agent SKILL in plain language, covering both the CLI and the MCP tools.

### Cleanup

- Avoid repeated per-reminder tag and subtask-count queries when batch reads find no extras.
- Reduce repeated work in ASCII table-width measurement, limited EventKit read sorting, and Capability Host request handling.
- Close duplicated file handles if terminal or permission-channel setup fails. Slow Full Disk Access checks no longer hold the permission-status cache lock.
- Remove unused internal helpers and declarations.

## 1.8.0 — 2026-09-04

### Signed Capability Host

- Added a persistent, signed `RemCTL Capability Host.app` and per-user LaunchAgent. The host is the single macOS TCC identity for Full Disk Access, Reminders, and Automation, so Terminal, Hermes, Codex, and other callers no longer need separate grants.
- Normal `auto` mode routes all 49 permission-bearing commands through the owner-only Capability Host socket and protocol 2. Six setup and display commands remain local: `completion`, `doctor`, `list-symbols`, `onboard`, `permissions`, and `setup`. `REMCTL_CAPABILITY_HOST=force` requires the host; `REMCTL_CAPABILITY_HOST=direct` bypasses it for diagnostics and development.
- Hosted file inputs are opened by the caller and transferred as descriptor-backed capabilities. Interactive destructive confirmations, TTY behavior, stdout/stderr ordering, signals, and cancellation remain supported across the host boundary.
- **Correction to the 1.5.0 attachment-path guidance:** in hosted output, `attachments[].path` is host-verified metadata, not a caller-readable file capability. The 1.5.0 note below records the earlier direct-execution behavior; with the signed host, callers must use supported RemCTL delivery or rendering and must not receive Full Disk Access merely to open a protected path.
- The host runs the complete protected CLI from a sealed Python archive, verifies its signed parent and runtime before dispatch, and fixes the bridge, private helper, store, and scratch paths for each invocation. The public client remains compatible with Python 3.10+, while a live host install requires a protected Python 3.13+ runtime.
- `install.sh` now publishes the app, sealed runtime, secure socket, and LaunchAgent as one verified transaction, with rollback to the previous generation on failure. `uninstall.sh` uses guarded, identity-checked removal and refuses foreign or unexpected paths. Live installs preserve a stable signed identity so upgrades keep the same TCC grants.
- Installed bin files now have a hash-based ownership manifest. Upgrades and uninstall fail closed on modified, foreign, or unmanifested paths; `--adopt-existing-install` is a one-time, manually reviewed migration for exact official 1.7.1 files or an expected prerelease signed-host installation, not arbitrary older or custom files.
- `import` now accepts bounded piped JSON with `import -`, validates the complete document before writing, reports each confirmed JSON-mode success on stderr, returns one final JSON summary on stdout, and exits nonzero with `status: "partial"` when runtime failures leave a subset imported.

## 1.7.1 — 2026-08-13

- `show` now follows Reminders' persisted manual display order instead of always falling back to reminder creation order. The fix applies to JSON, plain, and table output, including each child list shown through a list group. Rows that have not merged into the ordering record yet remain visible after the positioned rows in their previous stable order.
- Expanded custom smart-list pin validation. The disposable private matrix now covers name and numeric-ID targeting, repeated idempotent pinning, current and protocol-1 payloads, positive/cleared `pinnedDate` readback, filter and identity preservation, built-in isolation, and cleanup.

## 1.7.0 — 2026-08-13

Added reminder ordering, required confirmation flags for scripted deletion, and fixed private API compatibility on Tahoe and Golden Gate.

### Reminder ordering

- Added `reminder-move --private` with `--before`, `--after`, `--first`, and `--last`. It changes display order without moving the reminder to another base list.
- Ordinary-list ordering and unsectioned, manually ordered custom smart lists are supported. Sectioned custom smart lists are refused before saving because their secondary-level ordering contract has not been verified.
- Every successful move re-reads the local Reminders store and returns `verified: true`. The helper protocol is now 2, so an older helper fails the existing preflight instead of receiving an unknown ordering payload.
- The design was informed by PR #26 from @davidgliu and implemented independently after cross-version ABI inspection and disposable write/readback testing.

### Deletion confirmation

- `delete`, `list-delete`, `section-delete`, `group-delete`, `smart-list-delete`, and `template-delete` now require `--force` whenever `--json` is present or stdin is not interactive.
- Without `--force`, JSON workflows receive a structured `confirmation_required` error on stderr, stdout stays empty, and no write occurs. Human confirmation prompts are written to stderr and retain their existing behavior.
- Fixes #27, reported by @tux234.

### Private ReminderKit compatibility

- Grocery fallback categorization preserves Tahoe's grocery-context/UUID contract and adds Golden Gate's list-change/`REMObjectID` contract. The unsafe Tahoe `REMObjectID` alternative is explicitly rejected by the implementation and audit.
- Built-in smart-list pinning no longer sends built-in IDs through the custom-list fetch. It keeps the generic path on hosts that expose it and fails before saving on hosts that do not. Custom smart-list pinning is unchanged.
- `remctl-private` now exposes a read-only `capabilities` action with watched selector availability, normalized encodings, the host OS, and `saveCalled: false`. The ordering selector set is included in protocol-2 capability reports.
- Corrected the `REMColor.colorSpace` declaration to `NSUInteger`, removed dead helper code, and documented the Tahoe/Golden Gate audit and validation boundaries.

## 1.6.1 — 2026-07-30

Fixed flag writes and added recurrence intervals and rules for specific weekdays of a month.

### Recurrence: intervals and Nth-weekday rules

- **`xN` interval token** right after the frequency, for every frequency: `daily x2`, `weekly x2 thu`, `monthly x3 15`, `yearly x2`. N is 1–999. The CLI now accepts intervals already supported by the bridge.
- **Monthly Nth-weekday rules**: `monthly 4th-fri` is the 4th Friday of each month, `monthly 1st-mon,3rd-mon` the 1st and 3rd Monday, `monthly last-fri` (alias of `-1-fri`) the last Friday, with negative forms down to `-5-fri`. Ordinal suffixes are validated (`4st-fri` is rejected), Nth-weekday tokens cannot be mixed with plain day-of-month numbers, and the forms are monthly-only. Prefer `last-fri` over `5th-fri`: EventKit silently skips months without a fifth Friday.
- **Round-trip.** Parsed rules carry a `weekNumbers` array parallel to `daysOfWeek`; `remctl-bridge` validates it before constructing `EKRecurrenceDayOfWeek` (out-of-range or wrong-frequency week numbers raise an uncatchable NSException inside EventKit, so they are rejected at the boundary) and emits it back on EventKit reads. Database reads report the pinning as `daysOfWeekDetailed` entries with `weekNumber`, as before. Human output renders `monthly 4th Fri`, `monthly last Fri`, `every 2 months 4th Tue`.
- **Occurrence-count rendering changed**: a rule ending after N occurrences now renders as `daily, 5 times` instead of `daily x5`, because `x5` now reads as the interval input token.
- **Hardened numeric parsing.** Recurrence digit tokens now use `isdecimal()` with bounded lengths; the old `isdigit()` path let `monthly ²` and multi-thousand-digit tokens raise tracebacks instead of a parse error.
- Design informed by PR #23 from @edequalsawesome; implemented fresh with validated ordinal suffixes and the `last-fri` alias.

### Flagging fixes

- **Fixed: flagging reminders in group-nested lists silently did nothing.** Reminders' AppleScript dictionary does not expose lists inside groups, so the old list-scoped script (`tell list "<name>"`) always failed for them with `-1728`; the command then fell back to remctl-bridge, which set priority as a "flag proxy" and reported success without ever touching the real flag. `flag`/`unflag` and `add --flag` now address the reminder at application level (`reminder id …`), which resolves reminders in every list, nested or not. (Reported by Brett Rosenberg.)
- **AppleScript errors are reported.** When the flag write fails, `flag`/`unflag` exit 1 with the underlying osascript error — under `--json`: `{"status": "error", "code": "applescript_flag_failed", …}` on stderr — instead of a success result. `add --flag` failures now include the error text in the stderr warning and a `warnings` array in the JSON payload.
- **Priority is never touched by flagging.** The bridge's priority=1 proxy is gone: `flag`/`unflag` no longer fall back to the bridge, and remctl-bridge now refuses `flag`/`unflag` actions and `flagged` payload fields outright instead of mutating priority (the old `unflag` proxy could wipe a High priority to none). Requires rebuilding the bridge via `./install.sh`.
- Regression coverage: app-level addressing, stderr surfacing, error-not-fallback on AppleScript failure for both commands, and the `add --flag` JSON warning.

## 1.6.0 — 2026-07-30

Write confirmations are now fully machine-readable: every `edit` outcome under `--json` identifies the reminder it touched, and the no-op path no longer breaks parsers.

- **`edit … --json` echoes `title`** on all four write paths (bridge, private-only, AppleScript fallback, and clone-delete moves between lists), matching the existing `add`/`done`/`undone`/`delete` convention. Agents and UI consumers can confirm a write without a follow-up `info` call.
- **Structured no-op result.** `edit` with nothing to change used to print the bare line `Nothing to update.` even under `--json`, breaking any strict parser. It now emits `{"status": "unchanged", "code": "nothing_to_update", "id": …, "title": …, "message": "Nothing to update."}` on stdout with exit 0. `status` is deliberately not `error`, so consumers that branch on error states keep working. Human (non-`--json`) output is byte-identical to before.
- Regression coverage for the title echo on every write path and for the structured no-op (including that it never falls through to AppleScript).

## 1.5.1 — 2026-07-18

- Fixed a startup crash on Python 3.14 caused by the literal percentage in the `--image-width` help text being interpreted as an argparse formatting token. Root help, diagnostics, and normal commands now construct the parser correctly across supported Python versions.
- Added regression coverage for formatting the root help text with the literal `~40%` default intact.

## 1.5.0 — 2026-07-18

Image attachments now include verified local paths in JSON and can appear as terminal previews.

### For agents

- **Attachments in JSON, everywhere.** `info --json` and every list command's JSON (`show`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `search`) now include an `attachments` array on any reminder that has attachments; the key is omitted when there are none. Subtask attachments remain `info`-only.
- **Verified file paths.** Each entry is `{filename, type, path, resolved, uti, width, height}`. `path` points at the actual file inside Reminders' group container (`Files/Account-*/Attachments/`), verified by hashing the file and matching it against the attachment's stored SHA-512 before it is reported. At the time of 1.5.0, vision-capable agents using direct execution could open the file directly. **Current hosted-flow note:** a signed-host caller receives verified path metadata but may not be able to open the protected file; see the 1.8.0 correction above. Legacy attachments that were never downloaded to this Mac report `path: null` and `resolved: false` — treat as unavailable, not as an error.
- **Batch-loaded.** List commands resolve attachments with constant-query batch preloads (no N+1), and path verification is memoized per process, so `show --json` on large lists stays fast.

### For people

- **Inline image previews.** `remctl info <id> --images` renders image attachments right in the terminal; list commands render with `--images --verbose`. Protocol auto-detection: Kitty graphics on Ghostty/Kitty/WezTerm/Konsole (PNG and JPEG passed through unmodified, other formats converted via `sips`), iTerm2 inline images (also Blink on iOS over SSH), and a truecolor half-block renderer elsewhere. Terminals without a usable protocol skip rendering — the plain filename lines always print.
- **Terminal-aware sizing.** Render width defaults to ~40% of the terminal's columns (capped 24–100 cells, half-block 64). Override with `--image-width N` or `REMCTL_IMAGE_WIDTH`.
- **Trailing badges in list output.** Human one-line summaries can now end with 🔗 (reminder has a rich link) and/or 🌄 (reminder has an image attachment), batch-loaded so they add no per-reminder queries. Badges appear in `show`, `search`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, group show, and subtask lines — never in JSON, CSV, table mode, or EventKit fallbacks.
- **Zero new dependencies.** Everything works on a stock macOS: rendering is stdlib-only, using Pillow if it happens to be installed and macOS `sips` with a built-in BMP decoder otherwise. First-time installs need nothing new.

### Rendering limits

- Renders only on a real TTY — never in pipes, `--json`, or table mode, so automation never sees escape sequences (`REMCTL_IMAGES_FORCE=1` exists as a test-only override).
- Files larger than 16 MB stay in JSON but skip rendering (`(preview unavailable)`).
- Legacy attachments that never synced to this Mac show `(file not downloaded on this Mac)` instead of failing.
- Global flags work before *and* after the subcommand (`remctl --images info 847` and `remctl info 847 --images` are equivalent); unknown `REMCTL_IMAGE_MODE`/`REMCTL_IMAGE_WIDTH` env values produce a one-line stderr warning instead of silent fallback.

### Notes

- Kitty escape sequences send `q=2` on the first chunk so terminals don't write graphics responses into your shell.
- ASCII-art rendering was removed during testing — modes are `kitty`, `iterm2`, `halfblock`, `none` only.
- Tests: 278 → 349, covering JSON shapes, path resolution and tamper rejection, schema drift, every render mode, the sips/BMP fallback, CLI guards, badges, batch-query counts, and flag parsing order.
