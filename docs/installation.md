# Installation and Onboarding

RemCTL is a copy-based install, not a Python package. The installer copies the CLI and helper files into a bin directory such as `~/bin` or `~/.local/bin`.

## Requirements

- macOS 14 or later
- Python 3.10 or later
- iCloud Reminders enabled
- Xcode Command Line Tools for the Swift write bridge

Install Xcode Command Line Tools if needed:

```bash
xcode-select --install
```

## Install

Default install to `~/bin`:

```bash
git clone https://github.com/viticci/remctl.git
cd remctl
./install.sh --bootstrap
```

Install to `~/.local/bin`:

```bash
PREFIX="$HOME/.local" ./install.sh --bootstrap
```

`--bootstrap` copies files, compiles `remctl-bridge` and `remctl-permissions` when `swiftc` is available, creates `~/.config/remctl/api-token`, and installs shell completion when supported.

It does not grant macOS permissions. Apple requires those grants to happen interactively.

It also does not run `doctor` by default. A new user should grant permissions first, then verify with `doctor` so the first health report is meaningful. For upgrades on an already-authorized Mac, use `./install.sh --doctor` if you want an immediate health check.

If the installer says `PATH action required`, add the printed line to your shell profile, then open a new Terminal window before typing `remctl`. The current Terminal keeps its old PATH until you start a new session. You can also run commands with the full installed path, such as `~/bin/remctl onboard`.

## First Run

```bash
remctl onboard
remctl permissions full-disk-access
remctl doctor
remctl today
```

`remctl onboard`:

1. Opens Reminders.app.
2. Triggers the native Reminders permission prompt.
3. Triggers the Automation prompt used by AppleScript fallback operations.
4. Checks direct database access.
5. Opens the guided Full Disk Access helper when needed.

`remctl permissions full-disk-access` is safe to run even if the direct CLI path already works. It is the clearest first-run path because it shows every Full Disk Access target visually before you run `doctor`. If the optional service is installed, onboarding may include both the CLI target and the local API service target; the service still needs `remctl service restart` after its row is verified.

## Full Disk Access

macOS does not provide a native Full Disk Access prompt for command-line tools.

Default visual flow:

```bash
remctl permissions full-disk-access
```

The helper opens System Settings, copies the first target path, shows draggable targets, and marks a target with a green check when that process can read the Reminders store. In the System Settings file picker:

1. Click `+`.
2. Drag a target row from the RemCTL helper into the picker.
3. If dragging is not accepted, press `Command-Shift-G`, paste the copied path, press Return, then click Open.
4. Run `remctl doctor` again.

The optional background service runs as a separate launchd process. If `local_api` is degraded with `database: not found`, run:

```bash
remctl permissions full-disk-access --scope service
```

Then run:

```bash
remctl service restart
remctl doctor
```

Manual fallback:

```bash
remctl doctor
remctl service status
```

Use the exact target printed by those commands. Open System Settings > Privacy & Security > Full Disk Access, click `+`, press `Command-Shift-G`, paste the path, press Return, then click Open.

## Optional Local API Service

Most users do not need the service. Install it only when you want the REST API or local fallback process.

```bash
remctl service install
remctl service status
```

Install during bootstrap:

```bash
./install.sh --bootstrap --with-service
```

Useful service commands:

```bash
remctl service restart
remctl service uninstall
remctl service install --port 8080
remctl service install --host 0.0.0.0
```

The launch agent lives at `~/Library/LaunchAgents/com.remctl.server.plist`. Logs go to `~/Library/Logs/remctl-server.log`.

## Upgrading

`git pull` updates the checkout only. It does not update the copied CLI in your `PATH`.

```bash
git pull
./install.sh
hash -r
remctl --version
remctl doctor
```

If you installed to `~/.local/bin`:

```bash
git pull
PREFIX="$HOME/.local" ./install.sh
hash -r
```

## PATH Checks

```bash
which remctl
remctl --version
remctl doctor
```

If `which remctl` does not find RemCTL after install, add the installer’s PATH line to your shell profile, then open a new Terminal window. If `which remctl` points at `~/.local/bin/remctl`, keep using `PREFIX="$HOME/.local"` for upgrades.

## Shell Completion

Recommended:

```bash
remctl setup --shell auto
```

Manual:

```bash
eval "$(remctl completion zsh)"
eval "$(remctl completion bash)"
remctl completion fish | source
```

## Manual Install

Use this only for custom setups:

```bash
mkdir -p ~/bin
cp remctl ~/bin/remctl && chmod +x ~/bin/remctl
cp remctl_runtime.py ~/bin/remctl_runtime.py
cp remctl_serialization.py ~/bin/remctl_serialization.py
swiftc -O -framework EventKit -framework Foundation -o ~/bin/remctl-bridge remctl-bridge.swift
swiftc -O -framework AppKit -framework Foundation -o ~/bin/remctl-permissions remctl-permissions.swift
cp remctl-server ~/bin/remctl-server && chmod +x ~/bin/remctl-server
~/bin/remctl setup --shell auto
~/bin/remctl onboard
~/bin/remctl permissions full-disk-access
~/bin/remctl doctor
```
