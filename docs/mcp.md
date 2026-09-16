# MCP Server

RemCTL 2.0 ships a local MCP (Model Context Protocol) server. MCP is the open protocol that AI apps such as Claude Code, Claude Desktop, Cowork, and Codex use to discover and call tools. With the server connected, an AI app sees typed reminder tools instead of having to learn the command line, and Claude Desktop renders results as an interactive reminders widget.

The server is `remctl mcp`. It runs over stdio (the AI app launches it as a child process), uses only the Python standard library, and executes every tool by running the installed `remctl` client with `--json`. Nothing changes in the permission model: the signed `RemCTL Capability Host.app` still owns Full Disk Access, Reminders, and Automation, and the AI app needs no grants of its own.

```text
Claude Code / Claude Desktop / Cowork / Codex / other MCP client
  -> remctl mcp (stdio, JSON-RPC, MCP 2026-07-28 + legacy initialize)
     -> remctl <command> --json  (one subprocess per tool call)
        -> signed RemCTL Capability Host over the owner-only socket
```

## Connect an AI app

Onboarding offers this automatically: `remctl onboard` ends with a "Connect AI apps" step that detects Claude Code, Codex, and Claude Desktop and asks before touching each one. The same step is available any time:

```bash
remctl mcp install                      # every detected app
remctl mcp install --client claude-code # one app
remctl mcp install --client codex
remctl mcp install --client claude-desktop
remctl mcp status                       # who is connected
remctl mcp remove --client codex        # disconnect
```

No configuration file is edited by hand:

| Client | What `remctl mcp install` does | Then |
| --- | --- | --- |
| Claude Code | Runs `claude mcp add --scope user --transport stdio remctl -- <python> <remctl> mcp`, replacing a stale entry. | New sessions see the server. In an open session, run `/mcp` to reconnect. Tools appear as `mcp__remctl__<tool>`. |
| Codex | Runs `codex mcp add remctl -- <python> <remctl> mcp`. | Codex CLI, the ChatGPT desktop app, and the IDE extension share that configuration. |
| Claude Desktop and Cowork | Merges `mcpServers.remctl` into `~/Library/Application Support/Claude/claude_desktop_config.json`, keeping every other key and saving a timestamped backup next to it. | Quit and reopen Claude Desktop. The server shows up in Claude chats and in Cowork on this Mac. |
| Other | Prints ready-to-paste JSON, TOML, and shell snippets (`remctl mcp config`). | Paste into the client's MCP settings. |

The launch command always uses an absolute Python interpreter (`remctl mcp config --format command` shows it) so GUI apps with a minimal `PATH` can start the server.

### One-click Claude Desktop extension

Claude Desktop's recommended way to add a local server is a desktop extension bundle (`.mcpb`). Build one that launches the RemCTL installed on this Mac:

```bash
remctl mcp bundle --open
```

The file lands in `~/Downloads/RemCTL.mcpb`. Double-click it (or drag it onto Claude Desktop) and click Install in the review dialog. The bundle is machine-specific because it points at your installed `remctl`; reinstalling RemCTL keeps it working. Use either the config route or the bundle route, not both.

### Verify

```bash
remctl mcp status
remctl doctor            # includes an mcp_clients check
```

In Claude Code, `claude mcp list` should report `remctl: ✔ Connected`. Ask "what's due today?" and the `today` tool runs.

## Tools

Tool names are stable identifiers. Every tool returns `structuredContent` plus the same JSON as a text block, so clients that ignore structured results still get complete output. List results are wrapped as `{"items": [...], "count": N}`.

| Tool | Runs | Notes |
| --- | --- | --- |
| `today` | `today --json` | `include_overdue` (default true) |
| `upcoming` | `upcoming N --json` | `days` 1–365, default 7 |
| `overdue` | `overdue --json` | |
| `flagged` | `flagged --json` | |
| `search` | `search QUERY --json` | `include_completed` |
| `show_list` | `show LIST --json` | `list` or `list_id`; `include_completed` |
| `lists` | `lists --json` | ids, colors, badges, pin state |
| `get_reminder` | `info ID --json` | full record |
| `create_reminder` | `add ... --json -- TITLE` | `title`, `list`/`list_id`, `notes`, `due`, `priority`, `recurrence`, `alarm`, `url`, `tags`, `flagged` |
| `update_reminder` | `edit ID ... --json` | at least one field; `clear` removes due, recurrence, or alarm |
| `set_completion` | `done`/`undone ID --json` | `completed`, optional `completion_date` |
| `set_flagged` | `flag`/`unflag ID --json` | needs the host's Automation grant |
| `delete_reminder` | `delete ID --force --json` | destructive; annotated so clients confirm |
| `doctor` | `doctor --for-agent --json` | `access.effective` is authoritative |
| `run` | any argv | exact argv items, no shell; refuses `mcp`, `onboard`, `setup`, `permissions`, `completion`, `open`; optional `stdin` |

Every descriptor carries `title`, `description`, an `inputSchema` with `additionalProperties: false`, an `outputSchema` where the shape is fixed, and annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint: false`). Integer-like and boolean-like strings are accepted for typed arguments because widget actions and some models send them as text.

Errors follow the spec's two channels. A malformed request or unknown tool is a JSON-RPC error (`-32602`). A tool that ran but failed returns `isError: true` with `structuredContent.error` carrying RemCTL's own structured stderr error when it emitted one (for example `invalid_due_date` or `confirmation_required`), so the model can correct and retry.

Two prompts are included: `daily_review` and `plan_week`.

## Protocol support

The server is a dual-era MCP server as defined in the [2026-07-28 versioning rules](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning):

- **Modern (2026-07-28).** Every request carries `_meta["io.modelcontextprotocol/protocolVersion"]` and `_meta["io.modelcontextprotocol/clientCapabilities"]`. `server/discover` returns the supported versions, capabilities, instructions, and `serverInfo`. Every result has `resultType: "complete"` and `_meta["io.modelcontextprotocol/serverInfo"]`; list and read results carry `ttlMs` and `cacheScope`. A request naming another version gets `UnsupportedProtocolVersionError` (`-32022`) with the supported list; a 2026-07-28 request missing one of the two required fields gets `-32602` with the same list. A modern `initialize` is refused with `-32601` and the supported versions, so a legacy client that follows the hint can still fall back.
- **Legacy (2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05).** `initialize` echoes a requested version from that list, or answers `2025-11-25` for anything else. Results omit the modern fields; a missing resource is `-32002` as those revisions expect. JSON-RPC batches are accepted.
- Requests are served concurrently by a small worker pool. `notifications/cancelled` terminates the tool's subprocess. Tools are listed in a fixed order for prompt-cache stability. The process exits when stdin closes.

Verified against the official MCP Python SDK 2.2 client in `auto` (modern) and `legacy` modes, with and without MCP Apps, and against Claude Code and Codex.

## MCP Apps widget

RemCTL implements the [MCP Apps extension](https://modelcontextprotocol.io/extensions/apps/overview) (`io.modelcontextprotocol/ui`, spec 2026-01-26) with one self-contained widget, `ui://remctl/reminders-v1.html` (`text/html;profile=mcp-app`). The design and host bridge come from MacRemote's shared widget and were adapted to RemCTL's result shapes:

- **Reminder rows** (`today`, `upcoming`, `overdue`, `flagged`, `search`, `show_list`): Reminders-style check circles, title with flag, priority, and urgent marks, list subtitle when rows span lists, friendly due dates with overdue in red, a Repeats column when any row recurs, and section headers for `show_list`. Row actions: Mark Done, Reschedule and Edit Title (inline input), and Delete (two-step confirm).
- **Reminder card** (`get_reminder`): notes, tags, link, subtasks, alarms, and attachments count, with the same actions.
- **Change confirmation** (`create_reminder`, `update_reminder`, `set_completion`, `set_flagged`, `delete_reminder`): headline per status, resolved list, new id, warnings in amber (for example a created reminder whose flag step failed), and `partial` results.
- **Lists**, **doctor**, and a safe **summary** for `run`; `run` output that looks like reminder rows gets the rows treatment.

Linkage follows the spec's capability gating. A client that advertises `extensions["io.modelcontextprotocol/ui"].mimeTypes` containing the Apps MIME type (compared after RFC 9110 normalization, so quoting, spacing, and extra parameters are accepted) receives `_meta.ui.resourceUri` on tools and results, the resource in `resources/list`, and result hints under `_meta["net.macstories.remctl/ui"]`. Legacy clients that did not negotiate Apps receive only the deprecated flat `ui/resourceUri` and ChatGPT's `openai/*` aliases; modern clients without Apps receive nothing extra. `resources/read` always includes the sandbox policy (`prefersBorder`, empty CSP allowlists, `clipboardWrite`).

Widget actions are ordinary host-proxied `tools/call` requests to `set_completion`, `update_reminder`, and `delete_reminder`, so the host's approval flow applies. The widget loads no external resources, makes no network requests, never renders result text as HTML, and shows a visible Failed state with the CLI's error message instead of resetting silently.

## Troubleshooting

- `claude mcp list` shows `Failed to connect`: run `remctl mcp config --format command` and execute it in a terminal. It should wait silently for input; press Ctrl-D to exit. A traceback means the CLI itself is broken; run `remctl doctor`.
- The tools work in Claude Code but Claude Desktop shows nothing: quit and reopen Claude Desktop after `remctl mcp install --client claude-desktop`, or install the `.mcpb` bundle.
- `doctor` reports `mcp_clients` pointing at a different path: RemCTL moved (for example a new `PREFIX`). Run `remctl mcp install` again.
- A tool returns `capability_host` errors: the signed host is not ready. Call the `doctor` tool or run `remctl doctor` and follow its fix text; the MCP server never bypasses the host.
- Set `REMCTL_MCP_DEBUG=1` in the client's environment for a per-request trace on stderr.
