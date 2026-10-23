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

- Protected Python 3.13 ran 957 tests: 951 passed and six optional distribution checks were skipped. Python 3.14 passed 184 MCP, desktop-plugin, event, and orphaned-broker tests, plus the result-budget tests.
- Against the Developer ID build, 15 prebuilt distribution checks passed. The one remaining check expects a local signature and doesn't apply to a Developer ID app. Against a locally signed build of the same source, all five applicable prebuilt installer checks passed, including the default refusal of a local signature. The user keychain search list was identical before and after local signing.
- UI build, type checks, interaction contracts, and OpenAI extension schema checks passed. Claude Code strict plugin and marketplace validation passed, and its Today mod runner passed five tests.
- The CLI, both app version fields, the UI package and lockfile, and both plugin manifests report 2.4.0. Both plugin launchers share build identifier `cb1aa558e41e7a62`.
- The packaged client code matches the release source, except for the CLI's four-line protected-interpreter wrapper.

## Installed and live acceptance

This Mac was reinstalled from the release source with its existing Developer ID identity and protected Python runtime. A source build signs a new runtime, which needs an administrator password, so the installer was pointed at the existing runtime. Permission grants were preserved. Doctor reports 16 checks and zero failures. Its two warnings are Automation access, which macOS can't report until Reminders.app has run; only the AppleScript flag fallback needs it. The installed host's CDHash is `6bb646b84a2ed72f64c7dc60bd89447772e423a8`. The installed-surface audit passes all 41 comparisons, including both refreshed plugin caches.

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
| App submission | `bfe7795d-bfb9-4185-b2f8-ce946688ddfb` — Accepted |
| Disk image submission | `cb25515b-22a4-4731-930d-8db2074b4197` — Accepted |
| `RemCTL-arm64.dmg` SHA-256 | `b24de4aedd1b2931715d39fd9b5b2f32951e9e653d86ecdd366c4013f541d580` |
| Bundled Python | `3.13.15` |
| Bundled protected runtime ID | `72170c31e2d6c16aa5e80a13d4dc786c6cedd82d0172938afcbab2be36fd065a` |

Installing the download requires the normal administrator authorization for its new protected runtime. Existing permission grants are kept.

## Release refresh

The release was refreshed the same day, under the same version, to bring the help and documentation up to date and to fix an upgrade gap that the documentation review found.

- `smart-lists --help`, its command summary, the `subtasks` summary, and the splash screen describe Smart List sections and subtask nesting. One MCP field description was reworded.
- `SKILL.md` (the agent manual, which the personal `remctl` skills link to), the README, and the command, MCP, installation, and private-metadata guides cover ordered subtask creation and `subtaskPlacement`, verification of Smart List sections, write timeouts with `outcome: "unknown"`, `result_too_large`, when flagging falls back to Automation, and recovery from a host restart loop. A second model checked every added claim against the code over three review rounds.
- The installer refused to remove a socket that still answered, so the users affected by #66 couldn't upgrade. It now asks the new broker code to stop an identifiable orphaned broker first. That accepts the Python the old host recorded, so an install that ran on another interpreter is covered too. Live test: the host was stopped, and a fake orphaned broker was planted on the real socket. It ran on the protected Python, with the host's exact launch command, and launchd had adopted it. The installer stopped it, published 2.4.0, and `doctor` reported zero failures with private protocol 4.
- The refreshed artifact replaced the earlier one: the tag moved to the refreshed commit, and the release assets were replaced. Claude Code refreshes a plugin only when its version changes. An existing 2.4.0 Claude Code plugin keeps its earlier launcher file, which still runs the updated CLI. This Mac's cache was refreshed by reinstalling the plugin, and Codex picked up the new build through its normal update.

## Coverage limits

Testing ran on Apple silicon with macOS 27.0. The macOS 27.2 disk-image `sudo` fix, #62 on Intel, the Full Disk Access helper with a locally signed installed host (#67), and the launchd pended-spawn path (#64) are covered by unit tests, not live reproduction. The macOS 15 entity numbers (#74) were checked through the `Z_PRIMARYKEY` lookup on macOS 27 and with fixtures, not on a macOS 15 Mac. A physical iPhone and fresh native Codex and Claude Code agent conversations weren't tested; the plugins were exercised through their cached launchers.

## Publication

First published on October 23, 2026 from commit `b854bb6`. The same day, the refreshed release was republished from commit `e21975f`: the `v2.4.0` tag moved there, and the assets were replaced. The [public release](https://github.com/viticci/remctl/releases/tag/v2.4.0) is the latest release, not a draft or prerelease. Both public assets were downloaded without authentication; the refreshed disk image's bytes, GitHub asset digest, and SHA-256 match the verified build, the `latest` download URL serves it, and its stapled ticket and Gatekeeper assessment pass.

GitHub marks all eleven pull requests as merged by `viticci`, each with a thank-you comment that describes any changes made on top. Issues #65, #66, #67, and #75 were closed as completed by `viticci` with comments explaining the released behavior. Issues #17 and #48 and pull request #24 remain open by decision.
