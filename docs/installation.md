# Installation and Onboarding

RemCTL is not a Python package. The installer copies the CLI into a bin directory, builds and signs the Capability Host app, and installs a LaunchAgent that keeps the host running.

## Requirements

- macOS 14 or later. Release 2.0 is verified on the early macOS 27 build; the command paths also have test coverage on macOS 26.
- Python 3.10 or later for the CLI.
- A python.org Python 3.13 or later for the signed host. The official installer places it under `/Library/Frameworks/Python.framework`. The installer finds it; set `REMCTL_CAPABILITY_PYTHON` to a specific executable if it does not. A Homebrew Python works only if it passes the same ownership and permission checks (root-owned, not writable by other users, no ACLs).
- Xcode Command Line Tools, for the Swift and Objective-C helpers:

  ```bash
  xcode-select --install
  ```

- An `Apple Development` signing identity with a Team ID. Check with `security find-identity -v -p codesigning`. Xcode or an Apple Developer account can create one. RemCTL does not use ad-hoc signing for the host, because the grants are tied to the signature. Set `REMCTL_CODESIGN_IDENTITY` to a certificate hash to choose one.
- iCloud Reminders enabled.

## Install

```bash
git clone https://github.com/viticci/remctl.git
cd remctl
./install.sh --bootstrap
```

To install under `~/.local/bin` instead of `~/bin`:

```bash
PREFIX="$HOME/.local" ./install.sh --bootstrap
```

`--bootstrap` copies the CLI, compiles the helpers, builds the sealed host runtime, signs `~/Applications/RemCTL Capability Host.app`, installs `~/Library/LaunchAgents/net.macstories.remctl.capability-host.plist`, starts the host socket at `~/Library/Application Support/RemCTL/capability-host.sock`, creates `~/.config/remctl`, installs shell completion, and creates the `rctl` and `reminders` aliases. A custom `PREFIX` moves the app, the LaunchAgent, and the socket under that prefix; `remctl doctor` reports the paths.

The install is transactional. The installer stages a complete generation, verifies it, and only then replaces the previous one. It records file ownership in `.remctl-install-manifest.json`, and it restores the previous generation if anything fails.

If the installer prints `PATH action required`, add the line it shows to your shell profile and open a new terminal.

## Onboarding

```bash
remctl onboard
```

Onboarding is a guided flow. Each step explains what it does, asks before changing anything, and can be repeated. Running it again shows what is already set up and only asks about what is missing.

**Step 1: macOS permissions.** RemCTL reads and writes Reminders through the Capability Host, so the host needs three grants:

- Reminders. The host shows the standard macOS prompt.
- Automation for the Reminders app. Used for flags, which have no public API. The host shows the standard prompt.
- Full Disk Access, for the Reminders database. macOS has no prompt for this. If it is missing, RemCTL opens a helper that shows the exact app to add. See [Permissions](#permissions) for the manual steps.

The step lists each grant with a check mark or a fix.

**Step 2: Health check.** RemCTL confirms the host is ready and reads today's reminders.

**Step 3: Connect your AI apps.** RemCTL looks for Claude Code, Codex, and Claude Desktop on the Mac. For each one it finds, it asks whether to connect it, then registers the MCP server through that app's own mechanism. Apps that are already connected show a check mark. See [mcp.md](mcp.md).

**Step 4: Your other devices (optional).** This step appears only when Tailscale is installed. RemCTL offers to serve the MCP tools to your other tailnet devices over HTTPS with a private token, and prints the command to run on those devices. The default answer is no.

Flags: `--no-mcp` skips steps 3 and 4. `--no-tailscale` skips step 4. `--json` runs the permission checks and reports everything, including detected apps, without asking questions or opening the helper.

On a first run, the first data command you type (for example `remctl today`) also runs onboarding automatically when no onboarding state exists yet. That automatic run only does step 1 and prints a hint for the rest. `REMCTL_SKIP_ONBOARD=1` disables it.

After onboarding:

```bash
remctl doctor
remctl today
```

## Permissions

The Capability Host is the single macOS privacy target. Terminal, scripts, AI apps, and the MCP server use its grants through the owner-only socket. Do not grant Reminders, Automation, or Full Disk Access to Terminal, Python, Hermes, Codex, or Claude; they do not need it.

`remctl doctor --for-agent --json` reports two things: `access.direct` (what the current process could do on its own) and `access.effective` (what RemCTL can do through the host). `access.effective` is the one that matters. A blocked direct result is normal.

### Full Disk Access by hand

If the helper does not open, or you closed it:

```bash
remctl permissions full-disk-access
```

The helper opens System Settings and shows the exact host app path. In the Full Disk Access list:

1. Click `+`.
2. Drag the host row from the helper into the file picker, or press Command-Shift-G, paste the path (`~/Applications/RemCTL Capability Host.app` for a default install), press Return, and click Open.
3. Restart the host so it picks up the grant:

   ```bash
   launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
   ```

4. Run `remctl doctor`.

Restart the host only after changing Full Disk Access. Reminders and Automation grants take effect immediately.

### Automation state

The host caches the Automation result after it has seen a definitive answer (`authorized`, `denied`, or `notDetermined`). A freshly started host that cannot reach the Reminders app may report `targetNotRunning` or `unknown` until it verifies the state; `fullReady` stays false until then. Reminders does not need to stay open between commands.

### Limited reads without Full Disk Access

`show`, `search`, `today`, and `upcoming` accept `--via-eventkit`, a read-only path through EventKit that does not need Full Disk Access. It is never selected automatically. It returns `eventKitId` values, not RemCTL numeric ids, and omits sections, tags, private metadata, and table output. Use it for recovery, not as a setup.

## Connect AI apps

Onboarding offers this. The direct commands:

```bash
remctl mcp install                          # every app found on this Mac
remctl mcp install --client claude-desktop  # Claude Desktop and Cowork; restart Claude afterwards
remctl mcp install --client tailscale       # serve to your other devices over Tailscale
remctl mcp bundle --open                    # one-click .mcpb extension for Claude Desktop
remctl mcp status
```

The server needs no extra permissions because every tool runs the installed `remctl` through the host. [mcp.md](mcp.md) has the tool list, the Tailscale setup, and troubleshooting.

## Upgrading

`git pull` updates the checkout only. The installed copy is separate.

```bash
git pull
./install.sh
hash -r
remctl --version
remctl doctor
```

The installer keeps the host's signing identity, so its permissions carry over. Run `remctl onboard` again only if `doctor` reports a permission problem. If the tailnet endpoint is set up, the installer republishes the server code; restart the endpoint service with `remctl mcp install --client tailscale` or `launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.mcp-http"`.

For an install under `~/.local/bin`, keep the same prefix: `PREFIX="$HOME/.local" ./install.sh`.

### Upgrading from 1.7.1

Release 1.7.1 had no Capability Host and no ownership manifest. The first 2.0 install over it works like a first install:

1. Run `./install.sh` with the same prefix as the old install.
2. If it refuses because of unmanifested files, inspect every RemCTL path in that bin directory. Confirm they are the official 1.7.1 files, the `rctl` and `reminders` aliases, and the compiled helpers. Move anything else out of the way.
3. Run `./install.sh --adopt-existing-install` once.
4. Run `remctl onboard`, complete Full Disk Access if asked, restart the host, and run `remctl doctor`.

`--adopt-existing-install` accepts exactly the official 1.7.1 files or a reviewed prerelease host. It is not a general migration path and should not be used for routine upgrades.

## PATH

```bash
which remctl rctl reminders
remctl --version
```

If `which remctl` finds nothing, add the installer's PATH line to your shell profile and open a new terminal. If it finds `~/.local/bin/remctl`, keep using `PREFIX="$HOME/.local"` for upgrades.

## Shell completion

```bash
remctl setup --shell auto
```

For zsh, setup installs `_remctl` under `~/.zsh/completions` and prints the two lines to add to `~/.zshrc`:

```zsh
fpath=(~/.zsh/completions $fpath)
autoload -Uz compinit && compinit
```

`remctl doctor` warns (`completion_fpath`) when that directory is not on `fpath`. Manual alternatives:

```bash
eval "$(remctl completion zsh)"
eval "$(remctl completion bash)"
remctl completion fish | source
```

## Custom installation

Do not copy the script and helpers by hand. That skips the sealed runtime, the signed host identity, the LaunchAgent, and the socket. Use the installer overrides instead:

```bash
PREFIX="$HOME" \
REMCTL_BIN_DIR="$HOME/bin" \
REMCTL_APP_DIR="$HOME/Applications" \
REMCTL_LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents" \
REMCTL_CAPABILITY_PYTHON=/path/to/python3.13 \
./install.sh --bootstrap
```

Use the same overrides for every later upgrade.

## Uninstall

```bash
remctl mcp remove            # disconnect AI apps and the tailnet endpoint first
./uninstall.sh
```

The uninstaller checks that every file it removes belongs to RemCTL. It stops the host LaunchAgent, removes the app, the socket, the installed files, and empty completion directories. `--dry-run` shows the plan; `--keep-config` keeps `~/.config/remctl`. It does not edit your shell profile or revoke macOS permissions.
