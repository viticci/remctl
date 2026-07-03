# Installation and Onboarding

RemCTL is a copy-based install, not a Python package. The installer copies the CLI and helper files into a bin directory such as `~/bin` or `~/.local/bin`.

## Requirements

- macOS 14 or later
- Python 3.10 or later
- iCloud Reminders enabled
- Xcode Command Line Tools for the Swift write bridge, permission helper, and optional private ReminderKit helper

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

`--bootstrap` copies files, compiles `remctl-bridge` and `remctl-permissions` when `swiftc` is available, compiles the optional `remctl-private` helper when `clang` is available, creates `~/.config/remctl`, and installs shell completion when supported.

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

Private metadata writes do not require a separate first-run flow. They use the same Reminders permission grant as normal EventKit writes, but they also require the optional `remctl-private` binary installed next to `remctl`. `remctl doctor` reports this as `private_helper`. If it is missing, normal commands keep working; only `--private` writes are unavailable.

See [private-metadata.md](private-metadata.md) for supported private fields and examples.

`remctl permissions full-disk-access` is safe to run even if direct CLI reads already work. It is the clearest first-run path because it shows the Full Disk Access targets visually before you run `doctor`.

## macOS Permission Scope

macOS does not provide a native Full Disk Access prompt for command-line tools.

Full Disk Access and Reminders/EventKit authorization are scoped to the exact process context. The same Mac can have:

- Terminal green: `remctl doctor` passes from Terminal.
- Agent runner red: `remctl doctor` fails from Codex or another app runner.

That is normal TCC behavior, not a broken RemCTL install. Run `doctor` from the same context that will run the write. For agents, use:

```bash
remctl doctor --for-agent --json
```

Grant Full Disk Access to the target printed by that context. If the `eventkit` check fails, run `remctl onboard` from that same context and approve the Reminders prompt. Then relaunch the app or terminal that will run RemCTL.

If your terminal embeds another terminal engine, trust the `host_app` and target path from `doctor --for-agent`; RemCTL prefers the real bundle path over inherited variables such as `TERM_PROGRAM=ghostty`.

Default visual flow:

```bash
remctl permissions full-disk-access
```

The helper opens System Settings, copies the first target path, shows draggable targets, and marks a target with a green check when that process can read the Reminders store. In the System Settings file picker:

1. Click `+`.
2. Drag a target row from the RemCTL helper into the picker.
3. If dragging is not accepted, press `Command-Shift-G`, paste the copied path, press Return, then click Open.
4. Run `remctl doctor` again.

Manual fallback:

```bash
remctl doctor --for-agent
```

Use the exact target printed by `doctor`. Open System Settings > Privacy & Security > Full Disk Access, click `+`, press `Command-Shift-G`, paste the path, press Return, then click Open.

If an agent cannot get Full Disk Access or EventKit write access but the user's Terminal already passes `doctor`, a one-off Terminal relay can unblock testing: ask the user for approval, run the requested `remctl` command in Terminal via AppleScript, and capture stdout/stderr through temporary files. Do not treat that as the default automation path; the durable fix is granting access to the actual runner.

For basic reads only, `show`, `search`, `today`, and `upcoming` also accept `--via-eventkit` as a limited read-only fallback when a host cannot get Full Disk Access. This is not a setup replacement and should never be the default for agents. It omits RemCTL numeric IDs, sections, synced tags, private rich links, urgent state, template internals, smart-list internals, numeric list targeting, and table output. JSON returns `source: "eventkit"`, `fidelity: "limited"`, and per-item `eventKitId` values; those IDs cannot be passed to `info`, `edit`, `done`, `delete`, `link`, `open`, `subtasks`, or any numeric-ID command.

## Upgrading

`git pull` updates the checkout only. It does not update the copied CLI in your `PATH`.

```bash
git pull
./install.sh
hash -r
remctl --version
remctl doctor
```

Re-running `install.sh` also rebuilds the helper binaries. RemCTL checks a `remctl-private` protocol version on first `--private` use; an outdated helper refuses `--private` writes with a "re-run install.sh to rebuild" error, and `doctor` reports the helper protocol version.

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

If `which remctl` does not find RemCTL after install, add the installer's PATH line to your shell profile, then open a new Terminal window. If `which remctl` points at `~/.local/bin/remctl`, keep using `PREFIX="$HOME/.local"` for upgrades.

## Shell Completion

Recommended:

```bash
remctl setup --shell auto
```

For zsh, setup installs `_remctl` and prints the `fpath` lines that may need to be added to `~/.zshrc`:

```zsh
fpath=(~/.zsh/completions $fpath)
autoload -Uz compinit && compinit
```

`remctl doctor` reports `completion_fpath` when the installed zsh completion file does not appear in exported `FPATH` or the usual zsh startup files.

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
cp remctl_smart_lists.py ~/bin/remctl_smart_lists.py
swiftc -O -framework EventKit -framework Foundation -o ~/bin/remctl-bridge remctl-bridge.swift
swiftc -O -framework AppKit -framework Foundation -o ~/bin/remctl-permissions remctl-permissions.swift
clang -fobjc-arc -O -F/System/Library/PrivateFrameworks -framework Foundation -framework AppKit -framework ReminderKit -o ~/bin/remctl-private remctl-private.m
~/bin/remctl setup --shell auto
~/bin/remctl onboard
~/bin/remctl permissions full-disk-access
~/bin/remctl doctor
```
