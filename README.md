# RemCTL: The Power-User Reminders CLI

![RemCTL](https://cdn.macstories.net/cleanshot-2026-05-04-at-18-15-17-2x-1777911332976.png)

RemCTL is a fast, scriptable Reminders CLI for macOS designed for power users and AI agents.

RemCTL reads the user's local iCloud Reminders database directly (with native macOS permission access) for speed and detail, then writes through Apple's public EventKit APIs so changes sync normally to other devices.

Unlike other Reminders CLIs, RemCTL offers a special, optional integration with Reminders' Private API on macOS. This allows RemCTL to write proprietary metadata to reminders such as sections, subtasks, tags, image attachments, and location-based alarms by using the native ReminderKit framework.

As a result, RemCTL is the only Reminders CLI that truly replicates the modern Reminders experience on macOS 26 – without breaking iCloud sync.

## How It Works

```text
remctl
  reads:  ~/Library/Group Containers/group.com.apple.reminders/.../Data-*.sqlite
  writes: remctl-bridge -> EventKit
  private: remctl-private -> private ReminderKit APIs (--private only)
```

Why this architecture exists:

- **Direct SQLite reads** expose sections, subtasks, tags, attachments, deep links, list colors, and recurrence metadata in tens of milliseconds.
- **EventKit writes** keep Reminders and iCloud in charge of mutations. RemCTL does not write directly to the database.
- **Private metadata writes** are unsupported and explicitly opt-in with `--private`. They use Apple's private ReminderKit APIs, not direct SQLite mutation, and should be treated as experimental power-user functionality.

## Quick Start

The easiest experience is to ask your agent (Claude Code or Codex) to set up RemCTL for you by pointing it at this repo. Alternatively:

```bash
git clone https://github.com/viticci/remctl.git
cd remctl
./install.sh --bootstrap
remctl onboard
remctl permissions full-disk-access
remctl doctor
remctl today
```

`--bootstrap` copies the CLI and helpers, compiles the Swift helpers when `swiftc` is available, creates RemCTL's config directory, and installs shell completion when supported.

If the installer says `PATH action required`, add the printed PATH line and open a new Terminal window before typing `remctl`.

If you install to `~/.local/bin`, use the same prefix every time:

```bash
PREFIX="$HOME/.local" ./install.sh --bootstrap
```

Full setup details live in [docs/installation.md](docs/installation.md).

## Command Map

| Task | Commands |
| --- | --- |
| See what is due | `today`, `upcoming`, `overdue` |
| Browse reminders | `lists`, `show`, `search`, `flagged`, `urgent`, `info`, `subtasks` |
| Create and edit | `add`, `edit`, `done`, `undone`, `delete`, `flag`, `unflag` |
| Organize | `list-create`, `list-rename`, `list-delete`, `sections`, `tags` |
| Share data | `export`, `import`, `link`, `open`, `--json`, `--format table` |
| Set up the Mac | `onboard`, `permissions`, `doctor`, `setup`, `completion` |

Common examples:

```bash
remctl today
remctl show Work --format table
remctl add "Review PR" -l Work -d "tomorrow 10:00" -p high
remctl add "Pay rent" -d "2026-06-01" --recurrence monthly
remctl edit 23880 -d clear
remctl add "Research" -l Projects --private --url "https://example.com" -t remctl --new-section "Research"
remctl add "Launch assets" -l Projects --private --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"]}'
remctl info 23880 --json
```

The full command guide is in [docs/commands.md](docs/commands.md).

Due dates are atomic. If `-d/--due` is present and RemCTL cannot parse it, the command fails before creating or editing anything. Supported deterministic forms include `YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, `today at 3pm`, `tomorrow 09:30`, `tonight at 11`, `Friday at 15:00`, `next friday at 3pm`, `+3d`, `eod`, and `eow`.

## Private API Features

RemCTL's default writes use EventKit. For metadata Apple does not expose publicly, RemCTL has an explicit `--private` mode backed by `remctl-private`, an Objective-C helper that uses Apple's private ReminderKit framework and saves through the Reminders stack. RemCTL *never* writes directly to SQLite (which would break iCloud sync and cause database corruption issues).

Private writes are opt-in and power-user only:

```bash
remctl add "Research" -l Projects --private --url "https://example.com" -t remctl --section "Research"
remctl edit 23880 --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8
remctl add "Launch assets" -l Projects --private --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"]}'
remctl edit 23880 --private --image ~/Desktop/mockup.png --flagged --urgent
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200
```

Supported private metadata includes synced web rich links, synced tags, section assignment and creation, rich subtasks with per-child notes/due/URL/tags/images, image attachments, real flag state, urgent state, and location alarms. If a section name is duplicated in the same list, RemCTL picks the single non-empty match when there is one; otherwise use `--section-id`.

This is the major difference from ordinary EventKit-only Reminders CLIs, but it is still unsupported by Apple. Private-only flags fail before writing unless `--private` is present, generic file/PDF attachments are intentionally rejected, and agents should verify writes with `remctl info ID --json` plus a UI/device check when sync behavior matters.

## Output

RemCTL output is designed for both humans and agents:

- reminder IDs are shown as `#ID`
- `#ID` is colored with the reminder list color when RemCTL can read list colors
- flagged reminders show `⚑`
- macOS 26 urgent reminders show `⏰`
- recurring reminders show a repeat badge such as `↻ weekly Mon, Wed`
- table output keeps a dedicated `Repeat` column when any row is recurring
- every read command supports JSON output

```bash
remctl today --json
remctl --format table upcoming 14
NO_COLOR=1 remctl today
```

## macOS Permissions

RemCTL may need three macOS permission grants:

- Reminders access for EventKit writes
- Automation access for AppleScript fallback operations
- Full Disk Access for direct database reads

Run:

```bash
remctl onboard
remctl permissions full-disk-access
remctl doctor
```

The visual permission helper opens System Settings, copies the first target path, shows draggable targets for the current CLI process, and marks verified targets with a green check.

Full Disk Access is scoped to the process context. A Terminal session can pass `remctl doctor` while Codex, another agent runner, or a different host app fails. Run `remctl doctor` from the same context that will run RemCTL commands; for agent setup, use `remctl doctor --for-agent`.

Manual fallback: run `remctl doctor --for-agent`, then add the printed target in System Settings > Privacy & Security > Full Disk Access. In the file picker, press `Command-Shift-G`, paste the path, press Return, then click Open.

## For Agents

Use JSON when scripting:

```bash
remctl today --json
remctl show Work --json
remctl search "query" --completed --json
remctl info 23880 --json
remctl doctor --for-agent --json
```

`search` matches reminder titles and notes. By default it searches active reminders; pass `--completed` to include completed reminders too.

For fast agent writes, call `remctl add ... --json`, use the returned `numericId` when present, then verify with `remctl info <numericId> --json`. `info` includes private rich-link URLs, so agents should not need raw SQLite checks for ordinary rich-link verification.

Agents should pass deterministic due dates, ideally `YYYY-MM-DD HH:MM` after resolving the user's request in their timezone. If a due date is invalid, RemCTL exits before writing and emits a structured `invalid_due_date` JSON error on stderr with examples. Retry with a corrected date; do not create a reminder first and patch the due date afterward.

Do not mutate the Reminders SQLite database. Use RemCTL commands or EventKit.

For troubleshooting, trust the `context` object in `doctor --for-agent --json`. If Terminal is green but the agent runner is red, the install is not necessarily broken; grant Full Disk Access to the app or interpreter reported by the agent context, or run a one-off command through an already-authorized Terminal session.

After a repo update, reinstall the copied CLI before testing:

```bash
git pull
./install.sh
hash -r
remctl --version
remctl doctor
```

## Docs

- [Installation and onboarding](docs/installation.md)
- [Command guide](docs/commands.md)
- [Private metadata writes](docs/private-metadata.md)
- [Architecture](docs/architecture.md)

## Project Layout

| Path | Purpose |
| --- | --- |
| `remctl` | Main Python CLI |
| `remctl-bridge.swift` | Swift/EventKit write helper source |
| `remctl-private.m` | Unsupported private ReminderKit metadata helper source |
| `remctl-permissions.swift` | Swift/AppKit guided Full Disk Access helper source |
| `remctl_runtime.py` | Shared paths, config, date windows, safety helpers |
| `remctl_serialization.py` | Shared reminder JSON serialization |
| `install.sh` | Copy-based installer and bootstrap script |

## License

MIT. See [LICENSE](LICENSE).
