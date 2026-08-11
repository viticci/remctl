# Changelog

## Unreleased

- Added an optional, locally built `RemCTL Agent Helper.app` for agent hosts that should not receive Full Disk Access themselves. The helper bundles the installed RemCTL runtime, receives Reminders and database permissions, and accepts bounded argument arrays only through a custom macOS Apple Event. Each caller therefore needs a separate Automation grant; no network listener or Unix socket is exposed.
- Added the `remctl-agent` client, portable ad-hoc or developer signing, explicit macOS 14 deployment targeting, setup documentation, and regression checks for the helper bundle contract.

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
- **Verified file paths.** Each entry is `{filename, type, path, resolved, uti, width, height}`. `path` points at the actual file inside Reminders' group container (`Files/Account-*/Attachments/`), verified by hashing the file and matching it against the attachment's stored SHA-512 before it is reported. Vision-capable agents can open the file directly. Legacy attachments that were never downloaded to this Mac report `path: null` and `resolved: false` — treat as unavailable, not as an error.
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
