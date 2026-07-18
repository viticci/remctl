# Changelog

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
