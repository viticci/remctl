# Permission Onboarding Review

## Context

RemCTL needs to guide users through macOS privacy grants that cannot be granted from the command line. Full Disk Access is the worst case because the CLI process and the launchd `remctl-server` process may need different targets.

The immediate failure mode was:

- `remctl onboard` fixed or verified the direct database path.
- `remctl-server` still reported `local_api` as degraded with `database: not found`.
- The onboarding command did not open System Settings because it only checked the direct database result for Full Disk Access guidance.

## Permiso Review

Reviewed `zats/permiso` at commit `3012871` on 2026-05-04.

What it does well:

- Opens a System Settings privacy pane with `x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?...`.
- Tracks the frontmost System Settings window using `CGWindowListCopyWindowInfo`.
- Shows a non-activating overlay panel above System Settings.
- Provides a draggable item by writing a file URL to the pasteboard.
- Keeps the public API tiny: `PermisoAssistant.shared.present(panel: .accessibility)`.

Why RemCTL should not adopt it directly:

- The repo has no license file, so we should not vendor or fork its code into a public MIT project without explicit permission.
- It is app-bundle oriented. RemCTL needs arbitrary executable targets, especially Homebrew Python interpreters used by launchd.
- It only models Accessibility and Screen Recording. RemCTL needs Full Disk Access first.
- It supports one draggable item. RemCTL needs multiple targets in one flow: terminal app, CLI Python, service Python, and possibly helper binaries.
- It requires macOS 26 in `Package.swift`; RemCTL should not raise its minimum just for onboarding UI.
- It has only URL tests. There is no coverage for target resolution, drag payloads, Settings-window tracking, or fallback behavior.

## Decision

Do not adopt `permiso` as a dependency and do not fork it as-is.

Build a small RemCTL-owned Swift helper instead, using the same general pattern:

1. Open the correct System Settings privacy pane.
2. Locate the System Settings window.
3. Present a small overlay with draggable permission targets.
4. Provide a text fallback when the helper is unavailable.

This keeps licensing clean, allows executable-first behavior, and lets RemCTL ship features that are specific to a CLI plus launchd service.

## Proposed RemCTL Helper

Binary: `remctl-permissions`

Installed next to:

- `remctl`
- `remctl-bridge`
- `remctl-server`

Core concepts:

- `PermissionPanel`
  - `fullDiskAccess`
  - `accessibility`
  - `screenRecording`
- `PermissionTarget`
  - display name
  - absolute path
  - optional subtitle/rationale
  - icon from `NSWorkspace.shared.icon(forFile:)`
  - pasteboard payload as `.fileURL`
- `PermissionFlow`
  - title
  - ordered targets
  - commands to run after granting access

First RemCTL flow:

```bash
remctl permissions full-disk-access
```

or launched automatically from:

```bash
remctl onboard
```

The helper should show draggable rows for:

- the terminal app or current Python used by direct CLI reads
- the Python interpreter recorded in `~/Library/LaunchAgents/com.remctl.server.plist`
- optionally `remctl-server` as a fallback explanatory target if the interpreter cannot be resolved

Controls:

- Open System Settings
- Copy the selected executable path before the file picker opens
- Show the `Command-Shift-G` shortcut for pasting an absolute path into the file picker
- Reveal target in Finder
- Copy path
- Restart service
- Run doctor again

## Implementation Notes

- Keep the helper optional. If `swiftc` is unavailable, RemCTL should still print exact manual steps.
- Compile with AppKit/Foundation from `install.sh`, similar to `remctl-bridge`.
- Use the modern System Settings URL first:
  - `x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles`
- Keep the legacy URL as fallback:
  - `x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles`
- Do not attempt to edit TCC directly.
- Do not promise that granting access is complete until `remctl doctor` verifies it.
