# RemCTL 2.5.0 release validation

RemCTL 2.5.0 merges pull request #77 and fixes issues #76 and #78. The pull request was merged with its original commit, followed by maintainer commits that changed it.

## What changed on top of #77

The pull request's core fix was kept: a move into a shared list (`sharingStatus` not 0) goes straight to the ReminderKit copy, an EventKit save that fails with ReminderKit error `-3002` falls back to the same copy, the bridge returns the error's domain and code, and the original is deleted only after the copy's fields, rich links, and subtasks read back the same.

Two parts were changed:

- The pull request added an MCP `move_to_list` tool with a required `confirm`, and made `update_reminder` refuse `list` and `list_id`. The Codex workspace's Move to List menu sends `update_reminder` with `list_id`, so that menu would have stopped working. `update_reminder` keeps list moves, and no tool was added. A copying move already returned a new `id` and `oldId` before this release, and the deleted original goes to Recently Deleted.
- The combined-edit error named another product's tool (`reminders_move_to_list in Apple Core`). It now tells the user to move the reminder on its own first, then edit the returned id. The new `docs/list-moves.md` was folded into the existing guides.

## Issue #76

Rich links accept app links. Web links keep the public-host check from 1.0.3, which exists because a web link can point at a private address. An app link has no host to reach. `file:`, `javascript:`, `data:`, `about:`, `blob:`, `vbscript:`, and file-server or remote-shell schemes (`smb:`, `afp:`, `nfs:`, `ftp:`, `ftps:`, `sftp:`, `ssh:`, `telnet:`, `vnc:`, `ws:`, `wss:`) are refused. The client (`remctl_runtime.py`), the private helper, and the workspace each keep that list, and a test checks that the three match. The issue suggested a per-call `--allow-url-scheme` flag; a fixed blocklist does the same job without an extra step for agents.

Live testing found that the helper refused a bare app link such as `shortcuts://`, because NSURL reports no resource specifier for it, while the client had accepted it. The parent reminder was already created when the helper refused the subtask's link. The helper now checks the text after the colon, as the client does, and the case is covered by a test.

The workspace shows app links with an app icon and opens them through the host. It never fetches a web preview for them.

## Issue #78

The full workspace capped its height at 1,200 CSS pixels. In a Playwright window 1,800 pixels tall, the 2.4.0 workspace measured 1,200 pixels and the 2.5.0 workspace measured 1,800. The inline result card keeps its 700-pixel cap.

## Regression and package checks

- Protected Python 3.13 ran 973 tests: 965 passed, six optional distribution checks were skipped, and two failed. `test_staged_install_idempotence_rollback_and_uninstall` failed because the workspace was rebuilt while it ran; rerun alone, all 21 install lifecycle tests passed. `test_installer_accepts_the_old_hosts_python` fails only when the test interpreter is the Python the installed host recorded, as it is on this Mac; it fails the same way on `main` and passes on Python 3.14.
- Python 3.14 ran the same 973 tests: all passed except the six optional distribution checks, which were skipped.
- UI type checks, build, interaction contracts, and OpenAI extension schema checks passed.
- The CLI, both app version fields, the UI package and lockfile, the workspace app info, and both plugin manifests report 2.5.0. Both plugin launchers share build identifier `e0ca2677e426c805`.

## Installed and live acceptance

This Mac was reinstalled from the release commit with its existing Developer ID identity and protected Python runtime, so no administrator password was needed. Permission grants were preserved. Doctor reports zero failures and zero warnings. The installed host's CDHash is `47291b33710fd9871893915e17d964f691c09f32`. The Claude Code plugin updated from 2.4.0 to 2.5.0, and the Codex plugin cache was refreshed to 2.5.0.

Live checks used disposable lists `RL Test 250` and `RL Test 250 B`. With Federico's approval, one disposable reminder titled "RemCTL test - ignore" was moved into the shared Shopping list and back out.

| Path or case | Observed result |
| --- | --- |
| `lists --json` | `sharingStatus` 1 for Family and Shopping, 0 for every other list, matching their sharees |
| Parent with notes, timed due date, priority, flag, rich link, and two subtasks, moved between unshared lists | `clone-delete`, `subtasksMoved: 2`; every field, both links, and subtask notes read back; original gone |
| Plain reminder moved between unshared lists | In place, same id |
| Move into Shopping combined with `--title` | `list_move_requires_separate_edit`; nothing changed |
| Move into Shopping | `clone-delete`; notes, due date, and rich link read back in Shopping |
| Move out of Shopping to an unshared list | EventKit refused it; the fallback copied it back with `clone-delete` |
| `--private --url obsidian://…` with a subtask `url: shortcuts://` | Created; Reminders.app shows both as link chips (checked with cua-driver) |
| `edit --private --url shortcuts://run-shortcut?name=…` | Added; Reminders.app shows the chip next to the existing one |
| `file:///etc/hosts`, `example.com/page` | Refused before writing |
| MCP `update_reminder` with `list` | Moved; same id |
| MCP `create_reminder` with an `obsidian://` URL | Created with one rich link |
| MCP `create_reminder` with `javascript:` | Refused before writing |
| Codex plugin launcher (`REMCTL_PLUGIN=1`) | 2.5.0, 69 tools |

Both test lists and their reminders were deleted and verified absent. Shopping holds no test reminder. The copies the moves deleted are in Recently Deleted under the title "RemCTL test - ignore". No existing reminders were changed.

## Signed and notarized artifact

Both Developer ID apps and the disk image were accepted by Apple, stapled, and validated. A quarantined copy of the disk image passed its checksum and Gatekeeper as Notarized Developer ID, and so did the installer app inside it.

| Artifact | Verification value |
| --- | --- |
| App submission | `41a7dbc7-e215-439c-a45d-f5f765616750` — Accepted |
| Disk image submission | `8ce92b6b-29b5-42ad-9dee-aa4b30f1f0f8` — Accepted |
| `RemCTL-arm64.dmg` SHA-256 | `7f12f11308c6ef32410d11b56c41d7343ac8d702a928c8584cf7921490428dbd` |
| Bundled Python | `3.13.15` |
| Bundled protected runtime ID | `22dde0137f7afa4bbcbfa8a3d9290ea9a01fbf59c8add51a0e634568bca5d287` |

Installing the download requires the normal administrator authorization for its new protected runtime. Existing permission grants are kept.

## Coverage limits

Testing ran on Apple silicon with macOS 27.0. The shared-list move was tested on a list Federico owns, not on one shared with him. The workspace height was measured in a browser, not in a portrait Codex window. App links were checked as chips in Reminders.app, but no chip was clicked open.
