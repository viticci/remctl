# RemCTL 2.4.0 release validation

RemCTL 2.4.0 merges eleven contributor pull requests (#61, #62, #63, #64, #68, #69, #70, #71, #72, #73, #74) and fixes issues #65, #66, #67, and #75. Each pull request was merged with its original commit, followed by maintainer commits that changed or extended it. Issues #17 (Homebrew), #48 (a separate EventKit URL field), and pull request #24 (multi-account support) were deliberately left out of this release.

## How it was built

Three implementation branches were worked in parallel, each reviewed independently by a second model, fixed, and re-reviewed:

- **Store and MCP:** #74, #70, #73, #72, #71, and #65.
- **Host and installer:** #66, #64, #68, #67, #62, #69, and the disk-image `sudo` failure on macOS 27.2 reported in #63.
- **Private helper features:** #63, plus #61 and #75 together. Both new helper actions ship in private protocol 4.

Review findings that changed the code:

- The orphaned-broker takeover could have signalled a reused PID or an unrelated process. It now pins the process's kernel start time and requires the host's exact launch command.
- The 48 kB result cap skipped rows when paging, could loop on one oversized row, trimmed Codex workspace exports, and wasn't enforced for nested lists or long notes.
- Smart List section verification could accept a stale membership.
- `edit` resolved a section name twice, once before and once after its write.
- A recovered `add --flag` silently dropped the flag.
- The test suite could fall back to the installed private helper and EventKit bridge, which write to real Reminders. Tests now use stubs that refuse writes.

The implicit Smart List fallback in #61 was replaced with explicit `--smart-list`/`--smart-list-id` targeting. The signature cache in #68 keeps a passed check for 60 seconds instead of 10 minutes.

## Regression and package checks

- Protected Python 3.13 ran 954 tests: 948 passed and six optional distribution checks were skipped. Python 3.14 passed 184 MCP, desktop-plugin, event, and orphaned-broker tests, plus the result-budget tests.
- Against the Developer ID build, 15 prebuilt distribution checks passed. The one remaining check expects a local signature and doesn't apply to a Developer ID app. Against a locally signed build of the same source, all five applicable prebuilt installer checks passed, including the default refusal of a local signature. The user keychain search list was identical before and after local signing.
- UI build, type checks, interaction contracts, and OpenAI extension schema checks passed. Claude Code strict plugin and marketplace validation passed, and its Today mod runner passed five tests.
- The CLI, both app version fields, the UI package and lockfile, and both plugin manifests report 2.4.0. Both plugin launchers share build identifier `9ee5913f5632e45d`.
- The packaged client code matches the release source, except for the CLI's four-line protected-interpreter wrapper.

## Installed and live acceptance

This Mac was reinstalled from the release source with its existing Developer ID identity and protected Python runtime. A source build signs a new runtime, which needs an administrator password, so the installer was pointed at the existing runtime. Permission grants were preserved. Doctor reports 16 checks and zero failures. Its two warnings are Automation access, which macOS can't report until Reminders.app has run; only the AppleScript flag fallback needs it. The installed host's CDHash is `1c8c1f59e5d94c848e1882a36a724f7ed15e7953`. The installed-surface audit passes all 41 comparisons, including both refreshed plugin caches.

Live checks used disposable lists `RL Test 240` and `RL Test 240 B` and a disposable Smart List `RL Flagged 240`. Its two sections were created in Reminders.app through computer use.

| Path or case | Observed result |
| --- | --- |
| `add --subtask A --subtask B --subtask C` | A, B, C in order; `subtaskPlacement: first`; protocol 4 |
| `edit --subtask D --subtask E` | A–E in order; `afterExisting` |
| `reminder-move M --parent P` | `nested`, `verified: true`; same ID and notes |
| `--after A`, then `--first` | M between A and B, then first |
| Move from P to Q and back | M left P, then left Q, with no duplicates |
| `--top-level` with P in section S1 | `unnested`; M right after P, in S1 |
| Nest a parent; nest under a subtask | Both refused; nothing changed |
| `edit P -l "RL Test 240 B"` | `clone-delete`; subtasks still A–E |
| `smart-lists` | Both Smart List sections listed with IDs |
| `edit --smart-list … --section "Next Up"` | `verified: true`; Reminders.app shows the reminder under Next Up |
| Reassign by `--smart-list-id` and `--section-id` | `verified: true`; Reminders.app shows it only under Later |
| Missing section, no `--smart-list`, built-in Smart List | All refused before writing; no reminder created |
| Recurrence and alarm read-back (#74) | `daily` recurrence and the absolute alarm returned |
| Repeated `edit --url` (#73) | URL added once; a prefix URL still added separately |
| 16 concurrent reads (#68) | All succeeded; no signature failures. At 24, the existing 16-client limit returned `broker_busy`, never a signature error |
| Host killed with SIGKILL (#66) | Broker exited immediately; launchd started a new host; doctor ready with protocol 4 |
| MCP `show_list` with completed reminders (#72) | 173 kB trimmed to 47.7 kB of valid JSON, with `truncated` and 122 of 448 items reported |
| `doctor` in Terminal and over MCP (#71) | Same verdict: zero failures and the same two Automation warnings |

Flagging and Smart List sections were also exercised through every MCP surface, with Reminders.app closed:

| Surface | Result |
| --- | --- |
| Local `remctl mcp` | 2.4.0, 21 tools; Smart List section verified; unflag worked |
| Codex plugin cache | 2.4.0, 69 tools; flagged create, flag, private unflag, reads |
| Claude Code plugin cache | 2.4.0, 21 tools; same calls |
| Claude Desktop configured launcher | 2.4.0, 21 tools; same calls |
| Authenticated Tailscale MCP | 2.4.0, 21 tools; flagged create and Smart List section verified; an unauthenticated request returns 401 |

Reminders.app never launched during these MCP flag writes (#65), and every reminder created flagged read back as flagged. All test lists, the test Smart List, and their reminders were deleted and verified absent. No existing reminders were changed; large-result checks only read existing lists.

## Signed and notarized artifact

Both Developer ID apps and the disk image were accepted by Apple, stapled, and validated. A quarantined copy of the disk image passed Gatekeeper as Notarized Developer ID.

| Artifact | Verification value |
| --- | --- |
| App submission | `33e75528-32d9-47d7-bf6f-875e21c70316` — Accepted |
| Disk image submission | `6a83a884-2bfe-4ed3-bf58-464d583a5039` — Accepted |
| `RemCTL-arm64.dmg` SHA-256 | `c78dccc093a5fbe7fe3c6af5210e611346b48bf0177239b3964abb0353a3680f` |
| Bundled Python | `3.13.15` |
| Bundled protected runtime ID | `d56400ee81ac178912d0e158538f067d088db44a4e75b2b164bcfd88e4d3924a` |

Installing the download requires the normal administrator authorization for its new protected runtime. Existing permission grants are kept.

## Coverage limits

Testing ran on Apple silicon with macOS 27.0. The macOS 27.2 disk-image `sudo` fix, #62 on Intel, the Full Disk Access helper with a locally signed installed host (#67), and the launchd pended-spawn path (#64) are covered by unit tests, not live reproduction. The macOS 15 entity numbers (#74) were checked through the `Z_PRIMARYKEY` lookup on macOS 27 and with fixtures, not on a macOS 15 Mac. A physical iPhone and fresh native Codex and Claude Code agent conversations weren't tested; the plugins were exercised through their cached launchers.

## Publication

Published on October 23, 2026 from commit `b854bb6`, tagged `v2.4.0`. The [public release](https://github.com/viticci/remctl/releases/tag/v2.4.0) is the latest release, not a draft or prerelease. Both public assets were downloaded without authentication; the disk image's bytes, GitHub asset digest, and SHA-256 match the verified build, the `latest` download URL serves it, and its stapled ticket and Gatekeeper assessment pass.

GitHub marks all eleven pull requests as merged by `viticci`, each with a thank-you comment that describes any changes made on top. Issues #65, #66, #67, and #75 were closed as completed by `viticci` with comments explaining the released behavior. Issues #17 and #48 and pull request #24 remain open by decision.
