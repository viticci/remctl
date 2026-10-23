# Installation

RemCTL installs four things:

- The `remctl` command, in `~/bin`.
- **RemCTL Capability Host**, a signed app in `~/Applications` that holds the macOS permissions and runs every command that touches Reminders.
- A background service (a LaunchAgent) that starts the host when you log in.
- A private copy of Python under `/Library/RemCTL`, which only the host and the CLI use.

Both install routes produce the same result. The difference that matters is who signs the app: MacStories for the download, or a certificate on your Mac for your own build.

## Requirements

- macOS 14 or later, with iCloud Reminders turned on.
- Your Mac password, once, to install the protected Python.
- For your own build only: Apple's free Command Line Tools (`xcode-select --install`).

You don't need an Apple account, a developer membership, Homebrew, or your own Python.

### Renamed iCloud accounts

Renaming an iCloud account does not change where RemCTL can write. The signed host reads macOS account records and matches EventKit sources by stable account ID, including accounts nested under iCloud. It does not depend on the account's display name or need another permission.

If those records cannot be read, the existing iCloud-name fallback remains. A renamed account may then be refused; use `remctl doctor --for-agent` to check the host's access. Other account providers identified by macOS remain excluded even if their names contain “iCloud.”

## Download

Download `RemCTL-arm64.dmg` from [Releases](https://github.com/viticci/remctl/releases), open it, and double-click 'Install RemCTL'. macOS asks whether to open an app downloaded from the internet; click Open, and Terminal runs the installer. The download is for Macs with Apple silicon. On an Intel Mac, [build it yourself](#build-it-yourself); that route is designed for Intel too, but it hasn't been tested on one yet.

The installer copies 'RemCTL Capability Host' to `~/Applications` and starts it in the background. You don't open it yourself: it's the app that holds RemCTL's permissions. macOS shows a notice that software from "Federico Viticci" can run in the background; that's the host, and it's expected. When the installer finishes, you can eject the disk image and delete it.

If macOS says 'Install RemCTL.command' can't be opened, you have the 2.0.0 or 2.0.1 disk image. Download the latest release, or run `bash "/Volumes/RemCTL/Install RemCTL.command"` in Terminal.

From a checkout, this does the same thing:

```bash
./install.sh --bootstrap
```

The installer downloads the disk image for your Mac. Before it installs anything, it checks the app's full signature, confirms it was signed with the MacStories Developer ID, and asks Gatekeeper to accept it. If any of that fails, it stops. It never falls back to an unsigned build.

To install an app you already downloaded:

```bash
./install.sh --prebuilt '/path/to/RemCTL Capability Host.app' --bootstrap
```

## Build it yourself

```bash
xcode-select --install      # once, if you don't have the Command Line Tools
./install.sh --from-source --bootstrap
```

This one command downloads a pinned, checksum-verified Python, compiles the host and helpers, signs them, and installs the result. Build output goes in `.build/`; `--build-output DIRECTORY` picks another folder.

The first build creates a signing certificate without contacting Apple. It lives in `~/Library/Application Support/RemCTL Signing`, in its own keychain with owner-only files. RemCTL temporarily adds it to your keychain search list while signing, then restores the original list, including if signing fails. Concurrent RemCTL builds serialize this step with a per-user lock. **Keep this folder.** macOS ties your permissions to the certificate, so every future build must use the same one. If you lose it, you'll need to grant permissions again. Uninstalling RemCTL leaves it alone.

A few options for special cases:

- `REMCTL_CODESIGN_IDENTITY` signs with a certificate you already have.
- `REMCTL_SIGNING_DIRECTORY` keeps the RemCTL certificate somewhere else.
- `--prebuilt APP --allow-local-build` installs a local build you made separately. It still requires a real certificate signature; ad-hoc signatures are refused.

## Onboarding

`--bootstrap` creates RemCTL's config and, in an interactive Terminal window, goes straight into onboarding. If input or output is redirected, it prints the command to run later instead. You can always start (or resume) onboarding with:

```bash
remctl onboard
```

Onboarding explains each step, asks before changing anything, and skips what's already done.

1. **macOS permissions.** The host needs three: Reminders and Automation (both standard macOS prompts), and Full Disk Access, which has no prompt. If Full Disk Access is missing, RemCTL opens a helper that shows the exact app to add. See [Permissions](#permissions).
2. **Health check.** RemCTL confirms the host is ready and reads today's reminders.
3. **AI apps.** RemCTL checks the Claude Code command, the Codex app or command, Claude Desktop, and existing MCP connections. An enabled [Codex plugin](desktop-plugin.md) is reported as connected even without the `codex` command. If only the Codex app is installed, onboarding directs you to its plugin settings. ChatGPT is outside this automatic setup step. See [the MCP guide](mcp.md) for the supported connection routes.
4. **Other devices.** Only if Tailscale is installed: RemCTL offers to serve its tools to your other devices over your tailnet. The default answer is no.

`--no-mcp` skips steps 3 and 4, and `--no-tailscale` skips step 4. `--json` runs the permission checks and reports everything without asking questions.

If you skip onboarding, the first data command you run (say, `remctl today`) runs step 1 for you. `REMCTL_SKIP_ONBOARD=1` turns that off.

## Permissions

The Capability Host is the only app that needs permissions. The terminal, scripts, AI apps, and the MCP server all go through it over a socket only your user account can reach. Don't grant Reminders, Automation, or Full Disk Access to Terminal, Python, Codex, Claude, or Hermes; they don't need it.

`remctl doctor` checks everything. For scripts and agents, `remctl doctor --for-agent --json` reports two things: `access.direct` (what the current process could do by itself) and `access.effective` (what RemCTL can do through the host). Only `access.effective` matters. A blocked direct result is normal.

### Full Disk Access by hand

If the helper didn't open, or you closed it:

```bash
remctl permissions full-disk-access
```

The helper opens System Settings → Privacy & Security → Full Disk Access and shows the exact app. Click `+`, then either drag the app from the helper into the file picker, or press Command-Shift-G, paste the path (`~/Applications/RemCTL Capability Host.app` for a default install), press Return, and click 'Open'. Then restart the host so it picks up the change:

```bash
launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
remctl doctor
```

Only Full Disk Access needs a restart. Reminders and Automation take effect right away.

### Automation right after a restart

A host that just started may report Automation as `targetNotRunning` or `unknown` until it can check with the Reminders app. `doctor` waits up to three seconds, then reports a state it still can't read as a warning, not a failure: reads and writes work in the meantime, and the host clears it on its own. Reminders doesn't need to stay open. A refused grant is still a failure.

### Reading without Full Disk Access

`show`, `search`, `today`, and `upcoming` accept `--via-eventkit`, a read-only path that doesn't need Full Disk Access. RemCTL never picks it automatically. It returns `eventKitId` values instead of RemCTL's numeric ids, and it has no sections, tags, private metadata, or table output. It's meant for recovery, not everyday use.

## Upgrading

`git pull` only updates your checkout; the installed copy updates when you run the installer. Find your current setup below (`remctl --version` tells you the version).

| You have | Do this |
| --- | --- |
| RemCTL 2.0 from the download | Download the new release and open 'Install RemCTL' again, or run `./install.sh` from a checkout. |
| RemCTL 2.0 you built yourself | `git pull`, then `./install.sh --from-source`. |
| A 2.0 prerelease installed from `main` with your own Apple Development certificate | `git pull`, then `./install.sh --from-source`. |
| RemCTL 1.7.1 | See [Upgrading from 1.7.1](#upgrading-from-171). |
| RemCTL 1.7.0 or older | See [Upgrading from older versions](#upgrading-from-older-versions). |

Then check it:

```bash
hash -r
remctl --version
remctl doctor
```

An update that keeps the same signature keeps your permissions, so you only need `remctl onboard` again if `doctor` reports a problem. The prerelease row works because `--from-source` reuses the certificate your earlier install recorded. If you installed with a custom prefix, use the same one again (for example, `PREFIX="$HOME/.local" ./install.sh --from-source`).

After an upgrade:

- AI apps that already had RemCTL open keep running the old code until they reconnect or you start a new session.
- If the Tailscale endpoint is running, the installer restarts it and checks that it's healthy. If that fails, run `remctl mcp status` and repair it with `remctl mcp install --client tailscale`.
- Codex plugin users should refresh the plugin. See [Updating the plugin](desktop-plugin.md#update).

### Upgrading from 1.7.1

RemCTL 1.7.1 had no Capability Host and didn't record which files it installed. The installer recognizes the exact files 1.7.1 shipped and asks before replacing them. Open 'Install RemCTL' from the download, or run this from your checkout:

```bash
git pull
./install.sh --from-source --bootstrap
```

Answer yes when it asks to upgrade 1.7.1, and use the same prefix as your old install. If you changed any of those files, the installer can't verify them, so it lists them and offers to move them to the Trash instead. Your settings in `~/.config/remctl` stay either way. Without a terminal to answer in, for example from a script, add `--adopt-existing-install` to upgrade an exact 1.7.1.

Because the host is new, onboarding asks for permissions once. You can remove the Reminders and Full Disk Access grants you gave Terminal for 1.x afterwards; RemCTL doesn't use them anymore.

### Upgrading from older versions

Remove the old version with its own uninstaller before pulling, then install as new:

```bash
./uninstall.sh --keep-config
git pull
./install.sh --from-source --bootstrap
```

### Switching between the download and your own build

The download and your own build are signed by different certificates, and macOS treats them as different apps. The installer refuses to switch unless you ask:

```bash
./install.sh --migrate-signing                 # to the download
./install.sh --from-source --migrate-signing   # to your own build
```

Reminders and Automation prompt again during onboarding. Full Disk Access usually needs a manual fix, because macOS can keep the old app's entry (with the same name and the switch still on) and ignore the new one:

1. Open System Settings → Privacy & Security → Full Disk Access.
2. Select 'RemCTL Capability Host' and remove it with `−`.
3. Click `+`, add the exact app shown by `remctl permissions full-disk-access`, and turn it on.
4. Restart the host with the `launchctl kickstart` command above, then run `remctl doctor`.

If you use the Codex workspace, click 'Refresh' there once `doctor` passes.

## Where things go

| What | Default location |
| --- | --- |
| CLI and aliases (`rctl`, `reminders`) | `~/bin` |
| Capability Host | `~/Applications/RemCTL Capability Host.app` |
| Background service | `~/Library/LaunchAgents/net.macstories.remctl.capability-host.plist` |
| Socket | `~/Library/Application Support/RemCTL/capability-host.sock` |
| Settings | `~/.config/remctl` |
| Protected Python | `/Library/RemCTL/Python/<content-id>` |
| Build certificate | `~/Library/Application Support/RemCTL Signing` |

To install under `~/.local` instead, set `PREFIX` on every install and upgrade:

```bash
PREFIX="$HOME/.local" ./install.sh --from-source --bootstrap
```

A custom prefix moves the CLI, app, and socket. The LaunchAgent stays in `~/Library/LaunchAgents`, because that's where macOS looks at login. For finer control, `REMCTL_BIN_DIR`, `REMCTL_APP_DIR`, and `REMCTL_LAUNCH_AGENT_DIR` override each location; use the same overrides for every upgrade. The Codex plugin expects the default `~/bin/remctl`; see [the plugin guide](desktop-plugin.md#custom-install-locations) if you change it.

Don't copy files by hand. That skips the signed host, the protected Python, the background service, and the socket.

### How the installer protects you

The installer builds and verifies a complete new copy before it replaces anything. It records every file it owns in `.remctl-install-manifest.json`, refuses to overwrite files it doesn't recognize, and restores the previous version if anything fails, including starting the service. `--dry-run` builds and verifies without installing anything; it doesn't check permissions.

The protected Python is part of the same protection. The host only runs a Python whose every file matches a signed manifest, is owned by the system, and can't be changed by your user account. If that exact copy is already installed, the installer reuses it without asking for your password. If it's missing, the installer asks for your password through `sudo` (RemCTL never stores it). If it's damaged, the installer stops instead of asking. It never touches a system or Homebrew Python, and older copies stay in place for other installs that use them.

Maintainers can still point the legacy developer installer at an existing protected Python 3.13+ with `REMCTL_CAPABILITY_PYTHON`. It must be owned by root, not group- or world-writable, and have no ACLs. Normal installs don't need this. Some python.org installers leave the version folder group-writable; if the installer reports that, remove group write from that exact version tree (for example, `sudo chmod -R g-w /Library/Frameworks/Python.framework/Versions/3.13`) and check again after every Python update. Don't do this to Homebrew or other shared folders.

## PATH and shell completion

If `remctl` isn't found after installing, the installer printed a line like "Add /Users/you/bin to PATH". Add that folder to your shell profile and open a new Terminal window. `which remctl` shows which copy you're running; if it's `~/.local/bin/remctl`, keep using `PREFIX="$HOME/.local"` for upgrades.

The installer sets up shell completion (`--shell-completions none` skips it). To set it up again:

```bash
remctl setup --shell auto
```

For zsh, this installs `_remctl` under `~/.zsh/completions` and prints the two lines to add to `~/.zshrc`. `remctl doctor` warns (`completion_fpath`) if that folder isn't on your `fpath`. You can also load completion directly:

```bash
eval "$(remctl completion zsh)"
eval "$(remctl completion bash)"
remctl completion fish | source
```

## Uninstall

Disconnect AI apps and remove the Codex plugin first, because the plugin is served from inside the app:

```bash
remctl mcp remove
codex plugin remove remctl@remctl-local
codex plugin marketplace remove remctl-local
```

Then run the uninstaller from a checkout (`./uninstall.sh`) or from inside the installed app:

```bash
~/Applications/"RemCTL Capability Host.app"/Contents/Resources/Distribution/uninstall.sh
```

It checks that every file belongs to RemCTL before removing it. It stops the host and removes the app, the LaunchAgent, the socket, the CLI, and `~/.config/remctl`. `--keep-config` keeps your settings, and `--dry-run` shows the plan without changing anything. It leaves the protected Python and your build certificate in place, and it doesn't edit your shell profile or revoke macOS permissions.
