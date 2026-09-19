# Installation and Onboarding

RemCTL is not a Python package. The installer publishes the client and helper files into a bin directory, then builds and signs a persistent Capability Host app with a per-user LaunchAgent.

## Requirements

- macOS 14 or later for installation; the 1.8.0 signed-host release is verified on the current early macOS 27 Golden Gate build
- Python 3.10 or later for the client and source-tree commands
- A protected Python 3.13 or later runtime for the signed Capability Host
- iCloud Reminders enabled
- Xcode Command Line Tools for the Swift bridge, permission helper, Capability Host, and private ReminderKit helper
- An Apple Development signing identity with a TeamIdentifier for a live Capability Host install

Install Xcode Command Line Tools if needed:

```bash
xcode-select --install
```

## Install

Apple's Command Line Tools are sufficient to build RemCTL's Swift and Objective-C helpers. The full Xcode app is not required for the build itself.

The supported host runtime is a Python 3.13 or newer Framework installation from [python.org](https://www.python.org/downloads/macos/). Python's official macOS installer publishes the framework under `/Library/Frameworks/Python.framework` and requires an administrator account. RemCTL additionally verifies that the selected executable, its import roots, and their ancestry are root-owned, non-writable by untrusted users, and free of extended ACLs. Set `REMCTL_CAPABILITY_PYTHON` to the exact framework executable when automatic selection cannot find it. The sealed host archive is tied to that exact Python patch version, so rerun `./install.sh` after replacing or upgrading the selected runtime. A Homebrew Python may be detected, but it is accepted only if it passes the same protection checks.

A live install also needs an `Apple Development` signing identity whose signature supplies a TeamIdentifier and stable designated requirement. [Apple documents](https://developer.apple.com/help/account/create-certificates/certificates-overview) that development certificates can be created with Xcode or an Apple Developer account. Confirm that `security find-identity -v -p codesigning` lists an `Apple Development:` identity before installing. RemCTL does not use ad-hoc signing for a live host. Testers without a qualifying identity are not currently eligible for this install path; free-account behavior has not been validated. The installer auto-detects the first qualifying identity by default. Set `REMCTL_CODESIGN_IDENTITY` to its certificate hash when explicit selection is needed. The private-API compatibility audit additionally uses MachOSwiftSection's `swift-section`; it is an audit dependency, not a runtime dependency of `remctl`.

The minimum OS version is a build and installation floor, not a claim that every private API works there. The 1.8.0 signed-host release is verified on the current early macOS 27 Golden Gate build. The underlying command and private-API paths have historical coverage on macOS 26 Tahoe, but this exact signed-host release has not been rerun there. Run `remctl doctor --for-agent --json` and treat private ReminderKit features as version-sensitive on every other release.

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

With the default prefix, `--bootstrap` copies the client files, compiles the helpers, builds a sealed Capability Host runtime, signs `~/Applications/RemCTL Capability Host.app`, publishes `~/Library/LaunchAgents/net.macstories.remctl.capability-host.plist`, starts the owner-only socket at `~/Library/Application Support/RemCTL/capability-host.sock`, creates `~/.config/remctl`, installs shell completion when supported, and creates `rctl` and `reminders` aliases that behave identically to `remctl`. A custom `PREFIX` moves the app and socket below that prefix. The LaunchAgent always goes to `~/Library/LaunchAgents`, the only per-user directory launchd loads at login, unless `REMCTL_LAUNCH_AGENT_DIR` overrides it; reinstalling migrates a LaunchAgent that an earlier installer left below a custom prefix. `doctor` reports the exact installed paths.

The install is transactional. RemCTL stages and verifies a complete generation before publishing it, preserves the existing signing identity across upgrades, records installed-file ownership in `.remctl-install-manifest.json`, and restores the previous generation if app, service, transport, or verification publication fails. Later upgrades and uninstall refuse to replace changed or foreign artifacts.

It does not grant macOS permissions. Apple requires those grants to happen interactively.

It also does not run `doctor` by default. A new user should grant permissions first, then verify with `doctor` so the first health report is meaningful. For upgrades on an already-authorized Mac, use `./install.sh --doctor` if you want an immediate health check.

If the installer says `PATH action required`, add the printed line to your shell profile, then open a new Terminal window before typing `remctl`, `rctl`, or `reminders`. The current Terminal keeps its old PATH until you start a new session. You can also run commands with the full installed path, such as `~/bin/remctl onboard`.

## First Run

```bash
remctl onboard
```

Pause here if onboarding opens the Full Disk Access helper. Add only the signed `RemCTL Capability Host.app` that it shows. Full Disk Access is the one unavoidable manual step because macOS does not provide an approval prompt for it. If you changed the grant, restart the host:

```bash
launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
```

After any required restart, verify the installation and try a normal command:

```bash
remctl doctor
remctl today
```

For an agent setup, use `remctl doctor --for-agent --json` instead of the human-readable `doctor` command.

`remctl onboard`:

1. Activates the signed `RemCTL Capability Host.app` for an explicit permission request.
2. Triggers the host's native Reminders permission prompt.
3. Triggers the host's Automation prompt for Reminders AppleScript operations such as `flag` and `unflag`.
4. Checks the host's effective Full Disk Access and helper readiness.
5. Opens the guided Full Disk Access helper only when the exact host app still needs access.

If the helper does not open, do not restart the host or run a second permission command.

Private metadata writes do not require a separate first-run flow. They use the same host Reminders grant as normal EventKit writes and the `remctl-private` binary published in the signed generation. For hosted `auto` execution, `remctl doctor --for-agent --json` reports the authoritative helper state under `capabilityHost.privateProtocol.compatible`; the direct `private_helper` path/check applies only to explicit direct diagnostics. If the hosted protocol is incompatible, normal non-private commands can keep working, but `--private` writes are not ready until the signed generation is rebuilt.

See [private-metadata.md](private-metadata.md) for supported private fields and examples.

`remctl permissions full-disk-access` reopens the same exact-host guide. Use it to inspect or repair Full Disk Access after onboarding, not as an unconditional first-run step.

## macOS Permission Scope

macOS does not provide a native Full Disk Access prompt for command-line tools. RemCTL solves the per-caller permission problem with one stable signed identity: `RemCTL Capability Host.app` owns Full Disk Access, Reminders, and Automation. The default path is `~/Applications/RemCTL Capability Host.app`; a custom-prefix install uses the exact `capabilityHost.app.path` reported by `doctor`. Terminal, Hermes, Codex, and other installed callers use those grants through the owner-only socket and do not need separate Full Disk Access, Reminders, or Automation entries.

For agents, inspect both direct and effective access:

```bash
remctl doctor --for-agent --json
```

In normal `auto` mode, use `access.effective.route`, `access.effective.ready`, and `capabilityHost.fullReady` as the readiness gate. A direct caller can remain denied while the effective `capabilityHost` route is fully ready. Explicit `REMCTL_CAPABILITY_HOST=direct` bypasses the host and remains scoped to its caller; it is for diagnostics, development, and custom-store testing.

Reopen or repair the visual Full Disk Access flow:

```bash
remctl permissions full-disk-access
```

The helper opens System Settings and presents only the exact installed Capability Host. With the default prefix, that is `~/Applications/RemCTL Capability Host.app`. In the System Settings file picker:

1. Click `+`.
2. Drag the Capability Host row from the RemCTL helper into the picker.
3. If dragging is not accepted, press `Command-Shift-G`, enter the host path shown by the helper or `doctor`, press Return, then click Open.
4. Restart the host so the new Full Disk Access grant is applied: `launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"`.
5. Run `remctl doctor --for-agent --json` again and verify effective readiness.

Manual fallback:

```bash
remctl doctor --for-agent
```

Open System Settings > Privacy & Security > Full Disk Access, click `+`, press `Command-Shift-G`, enter `capabilityHost.app.path` from `doctor` (`~/Applications/RemCTL Capability Host.app` for the default install), press Return, then click Open. Do not add Terminal, Hermes, Codex, Python, or the `remctl` script for normal hosted use. After the grant changes, run `launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"`, then rerun `remctl doctor --for-agent --json`.

For basic reads only, `show`, `search`, `today`, and `upcoming` also accept `--via-eventkit` as a limited read-only fallback. The flag never changes routing and RemCTL never selects it automatically: normal installed `auto` mode runs the EventKit read inside the signed host, while explicit direct or custom-store execution runs it in the caller. This is not a setup replacement and should never be the default for agents. It omits RemCTL numeric IDs, sections, synced tags, private rich links, urgent state, template internals, smart-list internals, numeric list targeting, and table output. JSON returns `source: "eventkit"`, `fidelity: "limited"`, and per-item `eventKitId` values; those IDs cannot be passed to `info`, `edit`, `done`, `delete`, `link`, `open`, `subtasks`, or any numeric-ID command.

## Upgrading

`git pull` updates the checkout only. It does not update the copied CLI in your `PATH`.

```bash
git pull
./install.sh
hash -r
remctl --version
remctl doctor
```

The checkout and installed command are separate copies. Run `./remctl --version` before installation to identify the checkout, then run `remctl --version` after installation to identify the command found through `PATH`; both should report `1.8.0` for this release.

RemCTL 1.8.0 adds an ownership manifest for installed bin files. Its legacy migration recognizes only the exact public files shipped by the official 1.7.1 release, the expected aliases, and compiler-generated native helpers of the expected file types. Because native-helper bytes vary by compiler, their presence still requires a manual review and the one-time adoption flag after the normal upgrade refuses. Modified public files, another older version, and foreign paths are not treated as official 1.7.1.

Use this one-time adoption for an exact official 1.7.1 install with generated helpers, or for an expected prerelease signed-host installation, only after manual review:

1. Run the normal `./install.sh` command with the same `PREFIX` and overrides used for the old installation.
2. If it refuses unmanifested files, inspect every existing RemCTL path in that bin directory. For 1.7.1, confirm the exact release scripts, expected aliases, and native helpers. For a prerelease host, also confirm that its signed app and LaunchAgent belong to the expected installation. Move modified, unrelated, or unknown collisions out of the install destinations; do not adopt them.
3. Rerun once with the same environment and `./install.sh --adopt-existing-install`.
4. Run `remctl --version`. For an official 1.7.1 transition, run `remctl onboard`, complete any Full Disk Access step, restart the host after an FDA change, and only then run `remctl doctor --for-agent --json`. For a prerelease signed host that already had grants for the preserved identity, run doctor directly and use onboarding only if doctor reports permission trouble.

The adoption flag is an explicit trust decision for the exact recognized 1.7.1 generation or that reviewed prerelease host, not a generic migration for arbitrary old versions. It does not weaken app, LaunchAgent, file-type, signature, or path checks, and the successful install replaces the unmanifested generation with a new `.remctl-install-manifest.json`. Do not use it on routine later upgrades.

Re-running `install.sh` rebuilds the helpers and atomically replaces the signed host generation. It also rebuilds the sealed archive for the exact selected protected Python runtime. The installer preserves the signing identity, stops the previous LaunchAgent, verifies the new signature and protocol, starts the new job, and requires a working owner-only transport before finalizing. RemCTL separately checks the `remctl-private` protocol version on first `--private` use; an outdated helper refuses `--private` writes with a "re-run install.sh to rebuild" error. For hosted readiness, use `capabilityHost.privateProtocol.compatible`; use the direct `private_helper` result only during deliberate direct-mode diagnostics.

The official 1.7.1 release predates the signed host. After its first successful 1.8.0 install, run `remctl onboard`, complete the host's Reminders and Automation prompts, add the exact host to Full Disk Access if asked, restart the host after an FDA change, and run `remctl doctor --for-agent --json`. This transition cannot preserve host grants because 1.7.1 had no host identity.

The no-onboarding rule applies only when reinstalling or upgrading an existing signed-host installation whose identity is preserved, including later 1.8.x upgrades. In that case, start with `doctor`; rerun `onboard` only if it reports trouble with the host's Reminders, Automation, or Full Disk Access state. If you need to reopen the exact-host Full Disk Access guide during that repair, run `remctl permissions full-disk-access`.

If you installed to `~/.local/bin`:

```bash
git pull
PREFIX="$HOME/.local" ./install.sh
hash -r
```

## PATH Checks

```bash
which remctl
which rctl
which reminders
remctl --version
rctl --version
reminders --version
remctl doctor
```

If `which remctl` does not find RemCTL after install, add the installer's PATH line to your shell profile, then open a new Terminal window. If `which remctl` points at `~/.local/bin/remctl`, keep using `PREFIX="$HOME/.local"` for upgrades. The `rctl` and `reminders` aliases are installed in the same directory and require the same PATH entry.

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

## Custom Installation

Do not copy the script and helper binaries manually. That does not create the sealed runtime, stable signed TCC identity, LaunchAgent, or secure socket.

For a first custom install, use the supported installer overrides instead:

```bash
PREFIX="$HOME" \
REMCTL_BIN_DIR="$HOME/bin" \
REMCTL_APP_DIR="$HOME/Applications" \
REMCTL_LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents" \
REMCTL_CAPABILITY_PYTHON=/path/to/protected/python3.13 \
./install.sh --bootstrap
```

For later upgrades or reinstalls, use the same overrides and omit `--bootstrap`. The installer auto-selects an Apple Development identity when possible. Set `REMCTL_CODESIGN_IDENTITY` when it cannot find one or when you need another stable identity; upgrades must preserve the installed host's TeamIdentifier and designated requirement so its TCC identity does not change.

## Uninstall

```bash
./uninstall.sh
```

The uninstaller is guarded and identity-checked, not transactional. It verifies the installed app, sealed generation, service arguments, socket ownership, and marker paths before it stops the LaunchAgent and removes RemCTL-owned files. Use `./uninstall.sh --keep-config` to retain the RemCTL config directory. It refuses foreign or unexpected paths instead of deleting them. Uninstall does not reset the signed host's Full Disk Access, Reminders, or Automation grants; revoke those retained TCC entries manually in System Settings if desired.
