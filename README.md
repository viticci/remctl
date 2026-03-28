# RemCTL

Power-user CLI for Apple Reminders on macOS. Reads directly from the iCloud Reminders CoreData database for blazing-fast reads, writes via a Swift EventKit bridge for sub-100ms writes. Zero pip dependencies. Single file.

```
██████  ███████ ███    ███  ██████ ████████ ██
██   ██ ██      ████  ████ ██        ██    ██
██████  █████   ██ ████ ██ ██        ██    ██
██   ██ ██      ██  ██  ██ ██        ██    ██
██   ██ ███████ ██      ██  ██████   ██    ███████
▀▀▀▀▀▀ ▀▀▀▀▀▀▀ ▀▀      ▀▀  ▀▀▀▀▀▀  ▀▀    ▀▀▀▀▀▀▀
```

## Why RemCTL?

| Feature | RemCTL v3 | rem | remindctl |
|---------|-----------|-----|-----------|
| **Read speed** | ~42ms (SQLite) | ~160ms (EventKit) | ~160ms (EventKit) |
| **Write speed** | ~70ms (EventKit bridge) | ~75ms (EventKit) | ~80ms (EventKit) |
| **Sections** | Full read | None | None |
| **Subtasks** | Full read | None | None |
| **Tags/Hashtags** | Full read | None | None |
| **Attachments** | Full read | None | None |
| **Deep links** | Every reminder | None | None |
| **Color output** | Apple-colored per list | Basic | Basic |
| **Table format** | Unicode box-drawing | Limited | None |
| **Recurrence** | Set via bridge | Set | Set |
| **Alarms** | Set via bridge | Set | None |
| **JSON output** | Every command | Limited | None |
| **REST API** | Built-in server | None | None |
| **Dependencies** | Python 3 stdlib only | Swift only | Swift only |
| **Import/Export** | JSON + CSV | None | None |

## Installation

### Quick Install

```bash
git clone https://github.com/viticci/remctl.git
cd remctl
./install.sh
```

This installs:
- `~/bin/remctl` — Main CLI (Python)
- `~/bin/remctl-bridge` — Write helper (compiled Swift/EventKit)
- `~/bin/remctl-server` — REST API server (Python)

### Manual Install

```bash
# 1. Copy CLI
cp remctl ~/bin/remctl && chmod +x ~/bin/remctl

# 2. Compile and install bridge (requires Xcode CLT)
swiftc -O -framework EventKit -framework Foundation -o ~/bin/remctl-bridge remctl-bridge.swift

# 3. Copy API server
cp remctl-server ~/bin/remctl-server && chmod +x ~/bin/remctl-server
```

### Requirements

- macOS 14+ (Sonoma or later)
- Python 3.10+
- Xcode Command Line Tools (for compiling the Swift bridge)
- iCloud Reminders enabled

### Shell Completions

```bash
# zsh (add to ~/.zshrc)
eval "$(remctl completion zsh)"

# bash
eval "$(remctl completion bash)"

# fish
remctl completion fish | source
```

## Usage

### Quick Start

```bash
remctl                          # Splash screen + today's reminders
remctl today                    # Due today + overdue
remctl upcoming                 # Next 7 days, grouped by day
remctl upcoming 14              # Next 14 days
```

### Viewing Reminders

```bash
remctl lists                    # All lists with section counts
remctl show Shopping            # Reminders grouped by section
remctl show Work --completed    # Include completed items
remctl show Family -v           # Verbose (notes, URLs)
remctl info 23880               # Full detail for a reminder
remctl subtasks 23880           # View subtasks
remctl flagged                  # All flagged reminders
remctl overdue                  # All overdue reminders
remctl search "milk"            # Search by title or notes
remctl tags                     # All hashtag labels
remctl sections                 # Sections across all lists
remctl stats                    # Statistics overview
```

### Creating Reminders

```bash
remctl add "Buy milk"                               # Default list
remctl add "Review PR" -l Work                      # Specific list
remctl add "Call dentist" -d tomorrow                # Due tomorrow
remctl add "Team meeting" -d "next monday at 3pm"   # Natural language
remctl add "Deploy" -d +3d -p high                  # In 3 days, high priority
remctl add "Pay rent" -d "2026-04-01" -f            # Flagged
remctl add "Weekly report" --recurrence weekly       # Recurring
remctl add "Standup" --recurrence "weekly mon,wed,fri" --alarm 15m
remctl add "Check app" --url "https://example.com"  # With URL
remctl add "Groceries #shopping #weekly" -t errands  # With tags
```

#### Due Date Formats

| Format | Example | Result |
|--------|---------|--------|
| `today` | `-d today` | Today at midnight |
| `tomorrow` | `-d tomorrow` | Tomorrow |
| `+Nd` | `-d +3d` | 3 days from now |
| `+Nw` | `-d +1w` | 1 week from now |
| `eod` | `-d eod` | Today at 5:00 PM |
| `eow` | `-d eow` | Next Friday |
| `next <day>` | `-d "next friday"` | Next Friday |
| `next <day> at <time>` | `-d "next monday at 3pm"` | Next Monday at 3 PM |
| `in N days/weeks` | `-d "in 2 weeks"` | 2 weeks from now |
| ISO 8601 | `-d 2026-04-15` | April 15, 2026 |
| ISO + time | `-d "2026-04-15 14:00"` | April 15 at 2 PM |

### Modifying Reminders

```bash
remctl done 23880               # Complete
remctl undone 23880             # Uncomplete
remctl edit 23880 --title "New title" -d "next friday" -p medium
remctl edit 23880 -d clear      # Remove due date
remctl flag 23880               # Flag
remctl unflag 23880             # Unflag
remctl delete 23880             # Delete (with confirmation)
remctl delete 23880 --force     # Delete without confirmation
```

### List Management

```bash
remctl list-create "Project X" --color blue
remctl list-rename "Project X" "Project Y"
remctl list-delete "Project Y" --force
```

### Import/Export

```bash
# Export
remctl export --list Shopping --format json > shopping.json
remctl export --format csv > all-reminders.csv

# Import
remctl import shopping.json
```

### Deep Links

```bash
remctl link 23880               # Get deep link for a reminder
remctl link -l Shopping         # Deep links for all in a list
remctl open 23880               # Open in Reminders.app
remctl open                     # Just open Reminders.app
```

### Output Formats

```bash
remctl today                    # Plain text (default)
remctl --format table today     # Unicode table
remctl today --json             # JSON output
remctl --format json today      # Same as --json
```

Every command supports `--json` for machine-readable output.

### Color Output

RemCTL displays reminders color-coded by their list color in Reminders.app. Colors are automatically parsed from the iCloud database.

Disable colors:
```bash
remctl --no-color today         # Flag
NO_COLOR=1 remctl today         # Environment variable
```

## Architecture

```
┌──────────────────────────────────────────┐
│              remctl (Python)             │
│  ┌────────────┐    ┌──────────────────┐  │
│  │   SQLite    │    │  remctl-bridge   │  │
│  │   (reads)   │    │ (Swift/EventKit) │  │
│  │   ~42ms    │    │   writes ~70ms   │  │
│  └──────┬─────┘    └────────┬─────────┘  │
│         │                   │            │
│         ▼                   ▼            │
│  ┌──────────────────────────────────┐    │
│  │    iCloud Reminders Database     │    │
│  │   (syncs to all Apple devices)   │    │
│  └──────────────────────────────────┘    │
└──────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│         remctl-server (Python)           │
│  REST API over HTTP + Bearer auth        │
│  Tailscale-only access                   │
│  For future Android sync                 │
└──────────────────────────────────────────┘
```

### How Reads Work

RemCTL reads directly from the iCloud Reminders CoreData SQLite database at:
```
~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/Data-*.sqlite
```

This gives access to **everything** — sections, subtasks, tags, attachments, completion dates, deep link identifiers — in ~42ms. EventKit-based tools can only access basic fields and take 4x longer.

### How Writes Work

RemCTL uses a hybrid write path:

1. **remctl-bridge** (preferred): A pre-compiled Swift binary that writes via EventKit. ~70ms per operation. Supports recurrence, alarms, URLs, and list management.
2. **AppleScript fallback**: If the bridge isn't available, remctl falls back to AppleScript. Slower (~8.7s) and more limited, but works without compilation.

The bridge is detected automatically at `~/bin/remctl-bridge`.

## REST API Server

RemCTL includes a built-in REST API server for remote access and future Android sync.

### Starting the Server

```bash
remctl-server                       # Default port 19876
remctl-server --port 8080           # Custom port
remctl-server --generate-token      # Generate new auth token
```

### Authentication

All endpoints (except `/health`) require a Bearer token:
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://mac-studio.tailc0622.ts.net:19876/api/v1/today
```

The token is stored at `~/.config/remctl/api-token` and auto-generated on first run.

### API Endpoints

#### Read Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/lists` | All reminder lists with counts |
| `GET` | `/api/v1/lists/:name` | Reminders in a list |
| `GET` | `/api/v1/lists/:name/sections` | Sections for a list |
| `GET` | `/api/v1/reminders/:id` | Full reminder detail |
| `GET` | `/api/v1/reminders/:id/subtasks` | Subtasks |
| `GET` | `/api/v1/today` | Due today + overdue |
| `GET` | `/api/v1/upcoming?days=7` | Upcoming reminders |
| `GET` | `/api/v1/overdue` | Overdue reminders |
| `GET` | `/api/v1/flagged` | Flagged reminders |
| `GET` | `/api/v1/search?q=query` | Search reminders |
| `GET` | `/api/v1/tags` | All tags |
| `GET` | `/api/v1/sections` | All sections |
| `GET` | `/api/v1/stats` | Statistics |
| `GET` | `/health` | Health check (no auth) |

#### Write Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/reminders` | Create reminder |
| `PATCH` | `/api/v1/reminders/:id` | Update reminder |
| `DELETE` | `/api/v1/reminders/:id` | Delete reminder |
| `POST` | `/api/v1/reminders/:id/complete` | Mark complete |
| `POST` | `/api/v1/reminders/:id/uncomplete` | Mark incomplete |
| `POST` | `/api/v1/reminders/:id/flag` | Flag |
| `POST` | `/api/v1/reminders/:id/unflag` | Unflag |
| `POST` | `/api/v1/lists` | Create list |
| `PATCH` | `/api/v1/lists/:name` | Rename list |
| `DELETE` | `/api/v1/lists/:name` | Delete list |

#### Example: Create a Reminder

```bash
curl -X POST http://localhost:19876/api/v1/reminders \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Buy milk","list":"Shopping","dueDate":"2026-04-01T14:00:00","priority":"high"}'
```

Response:
```json
{
  "ok": true,
  "data": {
    "status": "created",
    "id": "A1B2C3D4-...",
    "title": "Buy milk"
  }
}
```

### Running as a Service

To run the API server persistently via launchd:

```bash
# Create a launchd plist (example)
cat > ~/Library/LaunchAgents/com.remctl.server.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.remctl.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/bin/remctl-server</string>
        <string>--port</string>
        <string>19876</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.remctl.server.plist
```

## Files

| File | Description | Size |
|------|-------------|------|
| `remctl` | Main CLI (Python 3, stdlib only) | ~1,800 lines |
| `remctl-bridge.swift` | Swift EventKit write helper (source) | ~370 lines |
| `remctl-bridge` | Compiled Swift binary | ~130 KB |
| `remctl-server` | REST API server (Python 3, stdlib only) | ~1,240 lines |
| `completions/_remctl` | zsh completion | ~100 lines |
| `install.sh` | Installer script | ~60 lines |

## License

MIT License. See [LICENSE](LICENSE).

## Author

Federico Viticci ([@viticci](https://github.com/viticci)) — [MacStories](https://www.macstories.net)
