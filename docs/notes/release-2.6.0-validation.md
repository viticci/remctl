# RemCTL 2.6.0 release validation

RemCTL 2.6.0 serves a `reminders` skill over MCP and fixes the hint that follows a list-write timeout. The release was built from commit `bdb2519` ("Prepare RemCTL 2.6.0").

## What changed

- The MCP server serves a `reminders` skill through the Skills extension (`io.modelcontextprotocol/skills`) to clients on MCP revision 2026-07-28. It declares the extension in `server/discover` and answers `skills/list`, `skills/get`, and `resources/read` for `skill://reminders/SKILL.md`, with the file's SHA-256 digest and byte size, over stdio and HTTP and in plugin mode. Sessions on older revisions are offered none of it. The skill's text lives in `remctl_mcp.py`, so the release ships it with no packaging change.
- When `create_list` or `update_list` times out, the hint now says to read the list back with `lists` or `get_list`. It used to point at `search` and `get_reminder`, which never show lists.

Nothing else changed. No reminder behavior changed, and the tool list is the same.

## Regression and package checks

- Python 3.14 ran 978 tests: all passed except the six optional distribution checks, which were skipped.
- Protected Python 3.13 ran the same 978 tests: 971 passed, six optional distribution checks were skipped, and one failed. `test_installer_accepts_the_old_hosts_python` fails only when the test interpreter is the Python the installed host recorded, as it is on this Mac, the same as in 2.5.0. Run alone, its file fails the same way on Python 3.13, and all nine of its tests pass on Python 3.14.
- UI type checks, interaction contracts, and OpenAI extension schema checks passed. Before the version change, the UI build reproduced the committed workspace byte for byte.
- Claude Code marketplace validation passed, and the Today mod's five tests passed. Strict validation of the Claude Code plugin fails, on its own test file and an icon: `tests/today.test.ts` has five type errors against the hook properties Claude Code 2.1.332 requires, and the new plugin-directory check reports that the plugin has no icon. The 2.5.0 plugin fails strict validation the same way under this Claude Code. Non-strict validation passes.
- The CLI, both app version fields, the UI package and lockfile, the workspace app info, and both plugin manifests report 2.6.0. Both plugin launchers share build identifier `e080a0a3ff93130f`.
- The packaged client code matches the release source, except for the CLI's four-line protected-interpreter wrapper.

## Installed and live acceptance

This Mac was reinstalled from the release commit with its existing Developer ID identity and protected Python runtime, so no administrator password was needed. Like every release build, the disk image carries a new protected runtime, which needs an administrator password, so the source installer was pointed at the existing runtime, as for 2.2.1 through 2.5.0. Permission grants were preserved. Doctor reports 16 checks, zero failures, and the same two warnings as before the update: Automation access, which macOS can't report until Reminders.app has run. The installed host's CDHash is `8a2a9ed545ef0388cc728d5744c105b698d547e2`. Gatekeeper accepts it as Developer ID, as it did the 2.5.0 host. The Capability Host and the MCP HTTP endpoint restarted with new process IDs. The installed-surface audit passes all 41 comparisons, including both refreshed plugin caches. The Claude Code plugin updated from 2.5.0 to 2.6.0 in both Claude Code profiles on this Mac, whose marketplace is this checkout, and the Codex plugin cache was refreshed to 2.6.0.

Live checks used one disposable list, `RL Test 260`, through a fresh installed `remctl mcp` server.

| Path or case | Observed result |
| --- | --- |
| Skills check, standard mode | Passed all 49 checks: one skill, `reminders`, one file of 8,177 bytes at `skill://reminders/SKILL.md`; a 2025-06-18 session sees no skill |
| Skills check, plugin mode (`REMCTL_PLUGIN=1`) | Passed all 49 checks, with the same skill |
| MCP tools, standard mode | 21 before and after, same names |
| MCP tools, plugin mode | 69 before and after, same names |
| `create_list` "RL Test 260" | Created |
| `create_reminder` "RemCTL test - ignore" | Created in RL Test 260 |
| `get_reminder` | Title and list read back, not completed |
| `set_completion` | Completed; read back with its completion date |
| `delete_reminder` | Deleted; `get_reminder` no longer finds it |
| `run list-delete --list-id … --force --json` | Deleted; `lists` and `get_list` no longer show it, and `search` with completed reminders finds nothing |

The deleted reminder is in Recently Deleted under the title "RemCTL test - ignore". No existing list or reminder was changed.

## Signed and notarized artifact

Both Developer ID apps and the disk image were accepted by Apple, stapled, and validated. A quarantined copy of the disk image passed its checksum and Gatekeeper as Notarized Developer ID, and so did the installer app inside it.

| Artifact | Verification value |
| --- | --- |
| App submission | `bb20c45b-e1df-42fe-a0d0-1a8af7e1df17` — Accepted |
| Disk image submission | `80faeaf2-7e76-4802-8425-1d7be64be9aa` — Accepted |
| `RemCTL-arm64.dmg` SHA-256 | `c8753a127044ef7230138fe8dab472ef3fd927d74fa7aac583c999b54b1bf6a1` |
| Bundled Python | `3.13.15` |
| Bundled protected runtime ID | `0756484676bad7035ff36b253bd4c508b81a66230b1e2ae174ebcbebef9687e7` |

Installing the download requires the normal administrator authorization for its new protected runtime. Existing permission grants are kept.

## Coverage limits

Testing ran on Apple silicon with macOS 27.2.1. No Claude or Claude Code session showed the skill. In Claude and Claude Code, skills over MCP are a public beta that Anthropic is still turning on account by account, and it is not on yet for this account, so Claude Code 2.1.332 doesn't ask any server for skills here. The skill was proven instead with a protocol checker that speaks MCP 2026-07-28 to the installed server: it checks the extension declaration, `skills/list`, `skills/get`, and `resources/read`, the file's digest and size, the `SKILL.md` frontmatter, Claude Code's size limits, and that an older session sees no skill. The installed server was checked over stdio; HTTP is covered by the tests, not by a live request to the installed endpoint. Codex wasn't checked for the skill. The new timeout hint is covered by tests, not by a live timeout. The disk image's own runtime wasn't installed on this Mac, because that needs an administrator password.

## Publication

Not published yet.
