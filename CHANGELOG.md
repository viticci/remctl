# Changelog

## Unreleased

### Fixed

- The Capability Host LaunchAgent now defaults to `~/Library/LaunchAgents` whatever the `PREFIX`. With a custom prefix such as the documented `PREFIX="$HOME/.local"`, the installer used to write it to `PREFIX/Library/LaunchAgents`, which launchd never loads at login, so the host stopped at the first reboot and every hosted command failed until the job was bootstrapped by hand. Reinstalling with the same `PREFIX` migrates an exact installer-owned plist from the old location, including while the old job is loaded; `uninstall.sh` still finds an unmigrated one. `REMCTL_LAUNCH_AGENT_DIR` still overrides the location.

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

This release adds verified reminder ordering, makes every destructive command safe in non-interactive workflows, and hardens private ReminderKit behavior across Tahoe and Golden Gate.

### Reminder ordering

- Added `reminder-move --private` with `--before`, `--after`, `--first`, and `--last`. It changes display order without moving the reminder to another base list.
- Ordinary-list ordering and unsectioned, manually ordered custom smart lists are supported. Sectioned custom smart lists are refused before saving because their secondary-level ordering contract has not been verified.
- Every successful move re-reads the local Reminders store and returns `verified: true`. The helper protocol is now 2, so an older helper fails the existing preflight instead of receiving an unknown ordering payload.
- The design was informed by PR #26 from @davidgliu and implemented independently after cross-version ABI inspection and disposable write/readback testing.

### Agent-safe destructive commands

- `delete`, `list-delete`, `section-delete`, `group-delete`, `smart-list-delete`, and `template-delete` now require `--force` whenever `--json` is present or stdin is not interactive.
- Without `--force`, JSON workflows receive a structured `confirmation_required` error on stderr, stdout stays empty, and no write occurs. Human confirmation prompts are written to stderr and retain their existing behavior.
- Fixes #27, reported by @tux234.

### Private ReminderKit compatibility

- Grocery fallback categorization preserves Tahoe's grocery-context/UUID contract and adds Golden Gate's list-change/`REMObjectID` contract. The unsafe Tahoe `REMObjectID` alternative is explicitly rejected by the implementation and audit.
- Built-in smart-list pinning no longer sends built-in IDs through the custom-list fetch. It keeps the generic path on hosts that expose it and fails before saving on hosts that do not. Custom smart-list pinning is unchanged.
- `remctl-private` now exposes a read-only `capabilities` action with watched selector availability, normalized encodings, the host OS, and `saveCalled: false`. The ordering selector set is included in protocol-2 capability reports.
- Corrected the `REMColor.colorSpace` declaration to `NSUInteger`, removed dead helper code, and documented the Tahoe/Golden Gate audit and validation boundaries.

## 1.6.1 — 2026-07-30

Two things: flagging is now honest end-to-end (it writes the real flag or fails — no more reporting success while writing nothing), and the recurrence grammar learned intervals and Nth-weekday rules.

### Recurrence: intervals and Nth-weekday rules

- **`xN` interval token** right after the frequency, for every frequency: `daily x2`, `weekly x2 thu`, `monthly x3 15`, `yearly x2`. N is 1–999. The bridge always supported intervals; the CLI grammar finally exposes them.
- **Monthly Nth-weekday rules**: `monthly 4th-fri` is the 4th Friday of each month, `monthly 1st-mon,3rd-mon` the 1st and 3rd Monday, `monthly last-fri` (alias of `-1-fri`) the last Friday, with negative forms down to `-5-fri`. Ordinal suffixes are validated (`4st-fri` is rejected), Nth-weekday tokens cannot be mixed with plain day-of-month numbers, and the forms are monthly-only. Prefer `last-fri` over `5th-fri`: EventKit silently skips months without a fifth Friday.
- **Round-trip.** Parsed rules carry a `weekNumbers` array parallel to `daysOfWeek`; `remctl-bridge` validates it before constructing `EKRecurrenceDayOfWeek` (out-of-range or wrong-frequency week numbers raise an uncatchable NSException inside EventKit, so they are rejected at the boundary) and emits it back on EventKit reads. Database reads surface the pinning as `daysOfWeekDetailed` entries with `weekNumber`, as before. Human output renders `monthly 4th Fri`, `monthly last Fri`, `every 2 months 4th Tue`.
- **Occurrence-count rendering changed**: a rule ending after N occurrences now renders as `daily, 5 times` instead of `daily x5`, because `x5` now reads as the interval input token.
- **Hardened numeric parsing.** Recurrence digit tokens now use `isdecimal()` with bounded lengths; the old `isdigit()` path let `monthly ²` and multi-thousand-digit tokens raise tracebacks instead of a parse error.
- Design informed by PR #23 from @edequalsawesome; implemented fresh with validated ordinal suffixes and the `last-fri` alias.

### Flagging: honest end-to-end

- **Fixed: flagging reminders in group-nested lists silently did nothing.** Reminders' AppleScript dictionary does not expose lists inside groups, so the old list-scoped script (`tell list "<name>"`) always failed for them with `-1728`; the command then fell back to remctl-bridge, which set priority as a "flag proxy" and reported success without ever touching the real flag. `flag`/`unflag` and `add --flag` now address the reminder at application level (`reminder id …`), which resolves reminders in every list, nested or not. (Reported by Brett Rosenberg.)
- **AppleScript errors are surfaced.** When the flag write fails, `flag`/`unflag` exit 1 with the underlying osascript error — under `--json`: `{"status": "error", "code": "applescript_flag_failed", …}` on stderr — instead of a fake success. `add --flag` failures now include the error text in the stderr warning and a `warnings` array in the JSON payload.
- **Priority is never touched by flagging.** The bridge's priority=1 proxy is gone: `flag`/`unflag` no longer fall back to the bridge, and remctl-bridge now refuses `flag`/`unflag` actions and `flagged` payload fields outright instead of mutating priority (the old `unflag` proxy could wipe a genuine High priority to none). Requires rebuilding the bridge via `./install.sh`.
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

Inline image attachments: reminders' images are now first-class data — as verified local file paths in JSON for agents, and as inline terminal previews for people.

### For agents

- **Attachments in JSON, everywhere.** `info --json` and every list command's JSON (`show`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `search`) now include an `attachments` array on any reminder that has attachments; the key is omitted when there are none. Subtask attachments remain `info`-only.
- **Verified file paths.** Each entry is `{filename, type, path, resolved, uti, width, height}`. `path` points at the actual file inside Reminders' group container (`Files/Account-*/Attachments/`), verified by hashing the file and matching it against the attachment's stored SHA-512 before it is reported. At the time of 1.5.0, vision-capable agents using direct execution could open the file directly. **Current hosted-flow note:** a signed-host caller receives verified path metadata but may not be able to open the protected file; see the 1.8.0 correction above. Legacy attachments that were never downloaded to this Mac report `path: null` and `resolved: false` — treat as unavailable, not as an error.
- **Batch-loaded.** List commands resolve attachments with constant-query batch preloads (no N+1), and path verification is memoized per process, so `show --json` on large lists stays fast.

### For people

- **Inline image previews.** `remctl info <id> --images` renders image attachments right in the terminal; list commands render with `--images --verbose`. Protocol auto-detection: Kitty graphics on Ghostty/Kitty/WezTerm/Konsole (PNG and JPEG passed through unmodified, other formats converted via `sips`), iTerm2 inline images (also Blink on iOS over SSH), and a truecolor half-block renderer elsewhere. Terminals without a usable protocol skip rendering — the plain filename lines always print.
- **Terminal-aware sizing.** Render width defaults to ~40% of the terminal's columns (capped 24–100 cells, half-block 64). Override with `--image-width N` or `REMCTL_IMAGE_WIDTH`.
- **Trailing badges in list output.** Human one-line summaries can now end with 🔗 (reminder has a rich link) and/or 🌄 (reminder has an image attachment), batch-loaded so they add no per-reminder queries. Badges appear in `show`, `search`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, group show, and subtask lines — never in JSON, CSV, table mode, or EventKit fallbacks.
- **Zero new dependencies.** Everything works on a stock macOS: rendering is stdlib-only, using Pillow if it happens to be installed and macOS `sips` with a built-in BMP decoder otherwise. First-time installs need nothing new.

### Safety guarantees

- Renders only on a real TTY — never in pipes, `--json`, or table mode, so automation never sees escape sequences (`REMCTL_IMAGES_FORCE=1` exists as a test-only override).
- Files larger than 16 MB stay in JSON but skip rendering (`(preview unavailable)`).
- Legacy attachments that never synced to this Mac show `(file not downloaded on this Mac)` instead of failing.
- Global flags work before *and* after the subcommand (`remctl --images info 847` and `remctl info 847 --images` are equivalent); unknown `REMCTL_IMAGE_MODE`/`REMCTL_IMAGE_WIDTH` env values produce a one-line stderr warning instead of silent fallback.

### Notes

- Kitty escape sequences send `q=2` on the first chunk so terminals don't write graphics responses into your shell.
- ASCII-art rendering was cut during dogfooding — modes are `kitty`, `iterm2`, `halfblock`, `none` only.
- Tests: 278 → 349, covering JSON shapes, path resolution and tamper rejection, schema drift, every render mode, the sips/BMP fallback, CLI guards, badges, batch-query counts, and flag parsing order.
