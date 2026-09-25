# RemCTL

![RemCTL](https://cdn.macstories.net/images/uploads/2026/05/26/cleanshot-2026-05-26-at-1629152x-1779805785287-9271e938c2.png)

RemCTL is a command-line tool and an MCP server for Apple Reminders on macOS. It is built for two kinds of users: people who work in a terminal, and AI apps that call tools.

- **The CLI.** `remctl today`, `remctl add "Buy milk" -d tomorrow`, `remctl show Work --json`. Fifty-six commands cover reminders, lists, sections, groups, smart lists, templates, import, and export.
- **The MCP server.** `remctl mcp` gives Claude Code, Claude Desktop, Cowork, Codex, and any other MCP client fifteen typed reminder tools, an interactive reminders widget, and access from your other devices over Tailscale.
- **One permission owner.** A signed app, `RemCTL Capability Host.app`, holds the macOS grants. Terminal, agents, and the MCP server go through it, so no other app needs Full Disk Access.

RemCTL reads the local Reminders database for speed and detail. It writes through Apple's public EventKit API, so changes sync through iCloud like any other edit. An optional `--private` mode writes Reminders-only metadata (sections, tags, subtasks, images, smart lists, templates) through Apple's private ReminderKit framework.

## Requirements

- macOS 14 or later. Release 2.0 is verified on the early macOS 27 build; the command paths also have test coverage on macOS 26.
- Python 3.10 or later for the CLI. A python.org Python 3.13 or later for the signed host (the installer finds it).
- Xcode Command Line Tools (`xcode-select --install`).
- An `Apple Development` signing identity. The installer signs the host app with it.
- iCloud Reminders enabled.

## Install

```bash
git clone https://github.com/viticci/remctl.git
cd remctl
./install.sh --bootstrap
~/bin/remctl onboard
```

`install.sh` copies the CLI to `~/bin`, builds and signs the Capability Host, installs its LaunchAgent, and installs shell completion. `remctl onboard` then walks you through four steps:

1. **macOS permissions.** The host asks for Reminders and Automation access. If Full Disk Access is missing, a helper shows the exact app to add in System Settings.
2. **Health check.** RemCTL confirms the host is ready and reads today's reminders.
3. **Connect your AI apps.** RemCTL finds Claude Code, Codex, and Claude Desktop on the Mac and asks, one at a time, whether to connect them. It uses each app's own settings; you never edit a config file.
4. **Your other devices (optional).** If Tailscale is installed, RemCTL offers to serve the same tools to your other tailnet devices over HTTPS with a private token.

Every step is optional after the first. Run `remctl onboard` again at any time; it shows what is already done and only asks about what is missing. Run `remctl doctor` to check the installation.

Details, including the manual Full Disk Access steps, are in [docs/installation.md](docs/installation.md).

## Use the CLI

```bash
remctl today                      # due today and overdue
remctl upcoming 7                 # the next week
remctl show Work --format table   # one list, in Reminders' order
remctl search "invoice" --json    # titles, notes, and saved links
remctl add "Review PR" -l Work -d "tomorrow 10:00" -p high
remctl add "Pay rent" -d 2026-06-01 --recurrence monthly
remctl edit 23880 -d clear
remctl done 23880 23881            # one id or a batch of up to 50
remctl delete 23880 --force
remctl info 23880 --json          # everything RemCTL knows about one reminder
```

Every read command has `--json`. Every reminder has a stable numeric `id` that works with `info`, `edit`, `done`, `undone`, `delete`, `link`, `open`, and `subtasks`.

| Task | Commands |
| --- | --- |
| See what is due | `today`, `upcoming`, `overdue`, `flagged`, `urgent` |
| Browse | `lists`, `list-info`, `groups`, `group-info`, `smart-lists`, `templates`, `template-info`, `show`, `search`, `info`, `subtasks`, `sections`, `tags`, `sharees`, `location-lookup`, `stats` |
| Create and edit | `add`, `edit`, `done`, `undone`, `delete`, `flag`, `unflag`, `reminder-move` |
| Organize | `list-create`, `list-edit`, `list-rename`, `list-delete`, `list-pin`, `list-unpin`, `list-symbols`, `section-create`, `section-rename`, `section-delete`, `group-create`, `group-edit`, `group-delete`, `smart-list-create`, `smart-list-edit`, `smart-list-delete`, `template-create`, `template-apply`, `template-delete` |
| Move data | `export`, `import`, `link`, `open` |
| Set up | `onboard`, `doctor`, `setup`, `permissions`, `completion` |
| AI apps | `mcp install`, `mcp status`, `mcp config`, `mcp bundle`, `mcp token`, `mcp remove` |

The command guide is [docs/commands.md](docs/commands.md). It covers due-date formats, recurrence rules, output formats, inline images, private metadata, and every command family.

The installer also creates `rctl` and `reminders` as aliases of `remctl`.

## Use it from AI apps (MCP)

MCP (Model Context Protocol) is the standard AI apps use to discover and call tools. `remctl mcp` is a local MCP server. It needs nothing beyond Python's standard library, and every tool runs the installed CLI with `--json`, so the Capability Host keeps owning the permissions and the AI app needs no grants of its own.

Connect an app (onboarding offers the same step):

```bash
remctl mcp install                          # every app found on this Mac
remctl mcp install --client claude-code     # uses `claude mcp add`
remctl mcp install --client codex           # uses `codex mcp add`
remctl mcp install --client claude-desktop  # Claude Desktop and Cowork; restart Claude afterwards
remctl mcp bundle --open                    # or a one-click .mcpb extension for Claude Desktop
remctl mcp status
```

Tools: `today`, `upcoming`, `overdue`, `flagged`, `search`, `show_list`, `lists`, `get_list`, `get_reminder`, `resolve_location`, `create_reminder`, `update_reminder`, `set_completion`, `set_flagged`, `delete_reminder`, `create_list`, `update_list`, `doctor`, and `run` (any other CLI command with exact arguments). Search is paged and can be scoped to one list; completion and deletion take batches; `private: true` unlocks synced tags, rich links, sections, subtasks, assignment, Early Reminders, and location alarms, including ones set from a street address. Each tool has a schema, annotations, and structured results. In Claude Desktop and other hosts that support MCP Apps, results render as a reminders widget with check-off, reschedule, rename, and delete.

Serve the same tools to your other devices:

```bash
remctl mcp install --client tailscale
```

This starts a small HTTPS endpoint that only devices on your Tailscale network can reach, protected by a private token. RemCTL prints the exact commands to run on the other device. See [docs/mcp.md](docs/mcp.md) for the tool reference, protocol details (MCP 2026-07-28 with compatibility back to 2024-11-05), the widget, and troubleshooting.

## How it works

```text
AI app (Claude Code, Claude Desktop, Cowork, Codex, other MCP clients)
  -> remctl mcp        stdio, or HTTPS over Tailscale
     -> remctl <command> --json

Terminal, scripts, agents
  -> remctl client (Python 3.10+)
     -> 7 setup commands run in the caller
     -> 51 data commands go over an owner-only socket to
        RemCTL Capability Host.app (signed, always running)
           reads:   the Reminders SQLite database (Full Disk Access)
           writes:  EventKit through remctl-bridge (Reminders access)
           flags:   AppleScript (Automation access)
           private: ReminderKit through remctl-private (--private only)
```

- Database reads return sections, subtasks, tags, attachments, deep links, list colors, recurrence, alarms, and Early Reminders in milliseconds. RemCTL opens the database read-only and never writes to it.
- EventKit writes keep Reminders and iCloud in charge of sync.
- The host is the only macOS privacy target. Its signature stays the same across upgrades, so the grants survive.
- `REMCTL_CAPABILITY_HOST=auto` (default) uses the host when installed. `force` requires it. `direct` bypasses it for diagnostics, in which case the caller needs its own permissions.

[docs/architecture.md](docs/architecture.md) has the full picture, including the data model.

## Permissions

The Capability Host needs three grants: Reminders, Automation for the Reminders app, and Full Disk Access. `remctl onboard` requests the first two and guides you through the third, which macOS only allows by hand. If you change Full Disk Access later, restart the host:

```bash
launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
```

Do not grant these permissions to Terminal, Python, or an AI app. They are not needed. [docs/installation.md](docs/installation.md#permissions) has the details.

## Private metadata

Some Reminders features have no public API: sections, synced tags, rich links, image attachments, subtasks with their own metadata, shared-list assignment, urgent state, Early Reminders, manual ordering, list icons and emoji, Groceries lists, list groups, custom smart lists, and templates. RemCTL writes them only when you pass `--private`:

```bash
remctl add "Research" -l Projects --private --url https://example.com -t remctl --section Research
remctl edit 23880 --private --set-tags remctl,work
remctl smart-list-create "Priority or Today" --private --match any --priority high,medium --date today
```

These writes use Apple's private ReminderKit framework through a small helper. They never touch the database directly. Apple can change them in any macOS release, so treat them as a power-user feature. [docs/private-metadata.md](docs/private-metadata.md) lists what is supported and how to verify each write.

## For agents

Read [SKILL.md](SKILL.md). In short:

- If the RemCTL MCP server is connected to your host, use its tools. They validate arguments and return the same numeric ids as the CLI.
- Otherwise call the installed CLI with `--json` and use the absolute path (`~/bin/remctl`).
- Use deterministic due dates (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM`). Pass `--force` to destructive commands. Verify writes with `info <id> --json`.
- `remctl doctor --for-agent --json` reports readiness. `access.effective` is the answer that matters.

## Upgrade

```bash
git pull
./install.sh
remctl doctor
```

The installer keeps the host's signing identity, so permissions carry over. Run `remctl onboard` again only if `doctor` reports a permission problem. Upgrading from 1.7.1, which had no host, needs the one-time steps in [docs/installation.md](docs/installation.md#upgrading).

## Uninstall

```bash
./uninstall.sh
```

The uninstaller stops the host, removes the app, the LaunchAgent, the socket, and the installed files. It does not revoke macOS permissions or edit your shell profile. Disconnect AI apps first with `remctl mcp remove`.

## Documentation

- [Installation and onboarding](docs/installation.md)
- [Command guide](docs/commands.md)
- [MCP server](docs/mcp.md)
- [Private metadata](docs/private-metadata.md)
- [Architecture](docs/architecture.md)
- [Agent manual](SKILL.md)
- [Changelog](CHANGELOG.md)

## Project layout

| Path | Purpose |
| --- | --- |
| `remctl` | The CLI |
| `remctl_mcp.py` | MCP server (stdio and HTTP), tool catalog, widget metadata, client connection helpers |
| `remctl_mcp_widget.html` | MCP Apps reminders widget |
| `remctl_broker.py` | Client and host sides of the socket protocol |
| `remctl_runtime.py` | Routing, paths, date windows, shared helpers |
| `remctl_serialization.py` | Reminder JSON |
| `remctl_images.py` | Attachment lookup and inline image rendering |
| `remctl_smart_lists.py` | Smart-list filter encoding |
| `remctl_capability_policy.py`, `remctl_capabilities.py` | Host command policy and descriptor-backed inputs |
| `remctl-bridge.swift` | EventKit write helper |
| `remctl-private.m` | Private ReminderKit helper |
| `remctl-capability-host.swift` | The signed host app |
| `remctl-permissions.swift` | Full Disk Access helper |
| `scripts/` | Archive builder and live test matrices |
| `install.sh`, `uninstall.sh` | Transactional installer and guarded uninstaller |

## License

MIT. See [LICENSE](LICENSE).
