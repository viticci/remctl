# RemCTL: The Power-User Reminders CLI

![RemCTL](https://cdn.macstories.net/cleanshot-2026-05-04-at-18-15-17-2x-1777911332976.png)

Fast, scriptable Apple Reminders for macOS.

RemCTL is a hybrid CLI: it reads the local iCloud Reminders database directly for speed and detail, then writes through Apple’s public EventKit APIs so your changes sync normally to iPhone, iPad, and Mac.

## How It Works

```
remctl
  reads:  ~/Library/Group Containers/group.com.apple.reminders/.../Data-*.sqlite
  writes: remctl-bridge -> EventKit
  serves: optional remctl-server -> localhost REST API
```

Why this architecture exists:

- **Direct SQLite reads** expose sections, subtasks, tags, attachments, deep links, list colors, and recurrence metadata in tens of milliseconds.
- **EventKit writes** keep Reminders and iCloud in charge of mutations. RemCTL does not write directly to the database.
- **The local API service is optional**. Use it only when you want HTTP access or a local fallback process.

## Quick Start

```bash
git clone https://github.com/viticci/remctl.git
cd remctl
./install.sh --bootstrap
remctl onboard
remctl permissions full-disk-access
remctl doctor
remctl today
```

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
| Set up the Mac | `onboard`, `permissions`, `doctor`, `setup`, `service`, `completion` |

Common examples:

```bash
remctl today
remctl show Work --format table
remctl add "Review PR" -l Work -d "tomorrow 10:00" -p high
remctl add "Pay rent" -d "2026-06-01" --recurrence monthly
remctl edit 23880 -d clear
remctl info 23880 --json
```

The full command guide is in [docs/commands.md](docs/commands.md).

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

## Optional Local API

The local REST API is served by `remctl-server` and defaults to `127.0.0.1:19876` with Bearer token auth.

```bash
remctl service install
remctl service status
curl -H "Authorization: Bearer $(cat ~/.config/remctl/api-token)" \
  http://127.0.0.1:19876/api/v1/today
```

API details are in [docs/rest-api.md](docs/rest-api.md).

## macOS Permissions

RemCTL may need three different macOS grants:

- Reminders access for EventKit writes
- Automation access for AppleScript fallback operations
- Full Disk Access for direct database reads

Run:

```bash
remctl onboard
remctl permissions full-disk-access
remctl doctor
```

The visual permission helper is the default Full Disk Access flow. It opens System Settings, copies the first target path, and shows draggable targets for the CLI and optional service.

If you only need to fix the background API service, run:

```bash
remctl permissions full-disk-access --scope service
```

Manual fallback: `remctl doctor` and `remctl service status` print the exact target paths. Open System Settings > Privacy & Security > Full Disk Access, click `+`, press `Command-Shift-G`, paste the printed path, press Return, then click Open.

The background API service is a separate launchd process, so it can need its own Full Disk Access grant. `remctl service status` prints the exact service target.

## For Agents

Use JSON when scripting:

```bash
remctl today --json
remctl show Work --json
remctl info 23880 --json
```

Do not mutate the Reminders SQLite database. Use RemCTL commands, the local API, or EventKit.

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
- [Architecture](docs/architecture.md)
- [REST API](docs/rest-api.md)

## Project Layout

| Path | Purpose |
| --- | --- |
| `remctl` | Main Python CLI |
| `remctl-bridge.swift` | Swift/EventKit write helper source |
| `remctl-permissions.swift` | Swift/AppKit guided Full Disk Access helper source |
| `remctl-server` | Optional localhost REST API |
| `remctl_runtime.py` | Shared paths, config, date windows, safety helpers |
| `remctl_serialization.py` | Shared reminder JSON serialization |
| `install.sh` | Copy-based installer and bootstrap script |

## License

MIT. See [LICENSE](LICENSE).
