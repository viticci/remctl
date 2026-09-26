# RemCTL

![RemCTL](https://cdn.macstories.net/images/uploads/2026/05/26/cleanshot-2026-05-26-at-1629152x-1779805785287-9271e938c2.png)

RemCTL reads and changes Apple Reminders from the terminal or an AI app through MCP (Model Context Protocol). It supports reminders, lists, sections, tags, subtasks, smart lists, templates, and import/export.

A signed app, **RemCTL Capability Host**, holds the macOS permissions. Terminal, Python, and AI apps use that host and need no separate grants. Reads use the local Reminders database; writes use Apple's EventKit API or, with `--private`, its private ReminderKit framework.

## Requirements

- macOS 14 or later. Release 2.0 is verified on the early macOS 27 build; the command paths also have test coverage on macOS 26.
- Python 3.10 or later for the CLI. A protected Python 3.13 or later for the signed host. Stock python.org installs may need [permission repair](docs/installation.md#protected-python-permissions).
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

The installer builds the signed host, installs the CLI in `~/bin`, and adds a background service. `onboard` guides permission setup, checks the installation, and offers to connect AI apps and other devices through Tailscale. Run `remctl doctor` to check readiness.

See [installation](docs/installation.md) for requirements, permissions, and custom paths.

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

The server provides 21 tools for reading, creating, and editing reminders and lists. Search supports pages and list filters; completion and deletion support batches. Recently Deleted can be inspected and reminders restored with their original IDs. Private metadata requires `private: true`. The `run` tool handles other data commands.

Clients that support MCP Apps can show a reminders widget with completion, rescheduling, renaming, and deletion. [The MCP guide](docs/mcp.md) covers tools and connections; [the Hermes guide](docs/hermes.md) covers Hermes Agent.

Serve the same tools to your other devices:

```bash
remctl mcp install --client tailscale
```

This starts a small HTTPS endpoint that only devices on your Tailscale network can reach, protected by a private token. RemCTL prints the exact commands to run on the other device. See [docs/mcp.md](docs/mcp.md) for the tool reference, protocol details (MCP 2026-07-28 with compatibility back to 2024-11-05), the widget, and troubleshooting.

## Permissions and architecture

The Capability Host needs Full Disk Access to read the database, Reminders access for EventKit, and Automation access for AppleScript. It starts at login and serves commands through a socket accessible only to your user account. RemCTL never writes directly to the database.

`onboard` requests Reminders and Automation access and explains how to grant Full Disk Access. After changing Full Disk Access, restart the host:

```bash
launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
```

See [permissions](docs/installation.md#permissions) and [architecture](docs/architecture.md).

## Private metadata

Some Reminders features have no public API: sections, synced tags, rich links, image attachments, subtasks with their own metadata, shared-list assignment, urgent state, Early Reminders, manual ordering, list icons and emoji, Groceries lists, list groups, custom smart lists, and templates. RemCTL writes them only when you pass `--private`:

```bash
remctl add "Research" -l Projects --private --url https://example.com -t remctl --section Research
remctl edit 23880 --private --set-tags remctl,work
remctl smart-list-create "Priority or Today" --private --match any --priority high,medium --date today
```

These writes use Apple's private ReminderKit framework through a small helper. They never touch the database directly. Apple can change these APIs in any macOS release. [docs/private-metadata.md](docs/private-metadata.md) lists what is supported and how to verify each write.

## For agents

Read [SKILL.md](SKILL.md).

- If the RemCTL MCP server is connected to your host, use its tools. They validate arguments and return the same numeric ids as the CLI.
- If MCP is unavailable, repair the connection. Use the CLI for setup, diagnostics, or an explicit CLI request.
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
- [Hermes Agent](docs/hermes.md)
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
