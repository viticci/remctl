# MCP Server

MCP (Model Context Protocol) is the open protocol AI apps use to discover and call tools. RemCTL ships a local MCP server, `remctl mcp`. With it connected, an AI app gets typed reminder tools instead of a command line to learn, and hosts that support MCP Apps show results as an interactive reminders widget.

The server uses only Python's standard library. Every tool runs the installed `remctl` command with `--json`. The signed Capability Host keeps owning the macOS permissions, so the AI app needs no grants of its own.

```text
AI app  ->  remctl mcp  ->  remctl <command> --json  ->  Capability Host  ->  Reminders
```

Two transports:

- **stdio.** The app launches `remctl mcp` as a child process. This is what Claude Code, Codex, Claude Desktop, and the [Codex plugin](desktop-plugin.md) use on this Mac.
- **Streamable HTTP over Tailscale.** A small HTTPS endpoint, reachable only from devices on your Tailscale network, for Claude Code, Codex, and other clients on your other devices.

## Connect an app on this Mac

`remctl onboard` offers this step. The same commands work any time:

```bash
remctl mcp install                          # every app found on this Mac
remctl mcp install --client claude-code
remctl mcp install --client codex
remctl mcp install --client claude-desktop
remctl mcp status
remctl mcp remove --client codex
```

| Client | What `remctl mcp install` does | Afterwards |
| --- | --- | --- |
| Claude Code | Runs `claude mcp add --scope user --transport stdio remctl -- <python> <remctl> mcp`. An older entry is replaced. With the [Claude Code plugin](../README.md#use-it-in-claude-code) enabled, it adds nothing and removes an older user-scope entry instead. | New sessions see the server. In an open session, type `/mcp` to reconnect. Tools appear as `mcp__remctl__<tool>`, or `mcp__plugin_remctl_remctl__<tool>` through the plugin. |
| Codex | Runs `codex mcp add remctl -- <python> <remctl> mcp`. With the [Codex plugin](desktop-plugin.md) enabled, it adds nothing and removes an older entry instead, because that entry hides the plugin's sidebar. | Codex clients that read the same configuration share that entry. Restart Codex after a removal. |
| Claude Desktop and Cowork | Adds `mcpServers.remctl` to `~/Library/Application Support/Claude/claude_desktop_config.json`. Every other key stays as it was, the file keeps its permissions, and a timestamped backup is saved next to it. | Quit and reopen Claude Desktop. The server appears in Claude chats and in Cowork on this Mac. |
| Hermes Agent | Nothing automatic. `remctl mcp config --format hermes` prints the `mcp_servers` entry for `~/.hermes/config.yaml`. | Paste it, then start a new Hermes session. See [hermes.md](hermes.md). |
| Other clients | `remctl mcp config` prints JSON, TOML, YAML, and shell snippets. | Paste into the client's MCP settings. |

The launch command uses an absolute Python path so GUI apps with a minimal `PATH` can start the server. For a normal install, that's RemCTL's protected Python, `/Library/RemCTL/Python/<content-id>/bin/python3.13`. `remctl mcp config --format command` shows the exact command. (Maintainer installs that use a Homebrew Python get the formula's stable `opt` path, not the versioned `Cellar` folder that `brew upgrade` deletes.)

### One-click Claude Desktop extension

Claude Desktop's preferred way to add a local server is an extension bundle (`.mcpb`). Build one that launches the RemCTL installed on this Mac:

```bash
remctl mcp bundle --open
```

The bundle is written to `~/Downloads/RemCTL.mcpb`. Double-click it, or drag it onto Claude Desktop, and click Install. It points at your installed `remctl`, so reinstalling RemCTL keeps it working. Use the config route or the bundle route, not both.

## Connect your other devices (Tailscale)

Tailscale is a private network between your devices. If it is installed on the Mac, RemCTL can serve the MCP tools to Claude Code, Codex, Claude Desktop, and Hermes on any other device in that network. Nothing is exposed to the public internet.

`remctl onboard` offers this step when Tailscale is detected. To run it directly:

```bash
remctl mcp install --client tailscale
```

This does three things:

1. Creates a private token in `~/.config/remctl/mcp-http.json` (file mode 0600).
2. Installs a LaunchAgent, `net.macstories.remctl.mcp-http`, that runs `remctl mcp serve --http` on `127.0.0.1:7362` and restarts it after reboots. The endpoint is bound to the loopback interface only.
3. Runs `tailscale serve --bg --https=443 --set-path=/remctl http://127.0.0.1:7362`. Tailscale terminates HTTPS with a certificate for your Mac's tailnet name and forwards only tailnet traffic.

The endpoint is then `https://<your-mac>.<tailnet>.ts.net/remctl`. Print the setup commands for another device any time:

```bash
remctl mcp config --format tailscale
```

The commands don't contain the token. Each device keeps the token in its own login Keychain, and each app reads it from there when it connects. The token never goes into a command, a shell file such as `~/.zshrc`, or an app's config file. Those files are often synced or committed along with other dotfiles.

### 1. Save the token on the other Mac

On the Mac with RemCTL, print the token:

```bash
remctl mcp token
```

On the other Mac, save it in the login Keychain. `security` asks for the token, so it stays out of your shell history:

```bash
security add-generic-password -U -a remctl -s remctl-mcp-token -w
```

After a rotation, run the same command again to replace the token.

### 2. Connect each app

Every app runs the same one-line helper when it connects. The helper reads the token from the Keychain and prints the `Authorization` header as JSON. If the Keychain item is missing, it fails instead of sending an empty token:

```sh
t=$(/usr/bin/security find-generic-password -s remctl-mcp-token -w) && printf '{"Authorization":"Bearer %s"}' "$t"
```

**Claude Code** runs it as `headersHelper`. `\u0027` is the JSON escape for a single quote, so the whole object fits in one single-quoted shell argument:

```bash
claude mcp add-json --scope user remctl '{"type":"http","url":"https://<your-mac>.<tailnet>.ts.net/remctl","headersHelper":"t=$(/usr/bin/security find-generic-password -s remctl-mcp-token -w) && printf \u0027{\"Authorization\":\"Bearer %s\"}\u0027 \"$t\""}'
```

**Codex** runs it as `http_headers_helper`. `codex mcp add` has no option for it, so add this table to `~/.codex/config.toml`:

```toml
[mcp_servers.remctl]
url = "https://<your-mac>.<tailnet>.ts.net/remctl"
http_headers_helper = "t=$(/usr/bin/security find-generic-password -s remctl-mcp-token -w) && printf '{\"Authorization\":\"Bearer %s\"}' \"$t\""
```

The token never becomes an environment variable, so the shell commands Codex runs for the model can't read it.

**Claude Desktop** can't connect to a private URL directly. It starts the `mcp-remote` bridge (needs Node) through `/bin/sh`, which reads the token from the Keychain first and hands it to the bridge in its environment, not its arguments. `remctl mcp config --format tailscale` prints the entry to merge into `claude_desktop_config.json`.

**Hermes Agent** reads the token through its `secrets.command` helper. See [hermes.md](hermes.md#other-devices).

**Other clients** need the URL and an `Authorization: Bearer <token>` header. Give them the token through their own secret store or header helper, not a file you sync or commit.

**Linux and other systems** have no `security` command. Save the token with the system's credential store, for example `secret-tool store --label="RemCTL MCP token" service remctl-mcp-token` on Linux, and replace the Keychain read in the helper with `secret-tool lookup service remctl-mcp-token`. Without a credential store, keep the token in a file only you can read (`chmod 600`), outside any folder you sync or commit, and read it with `cat`.

Requirements: Tailscale running and signed in on the Mac, MagicDNS enabled, and HTTPS certificates enabled for the tailnet (Tailscale admin console, DNS page). Onboarding and `remctl mcp install` explain exactly which of these is missing.

Manage the endpoint:

```bash
remctl mcp status                     # URL, service state, serve mount, health
remctl mcp token                      # print the token (sensitive)
remctl mcp token --rotate             # new token; save it on each device again
remctl mcp remove --client tailscale  # stop serving; the token file stays for later
```

`remctl doctor` reports the endpoint under `mcp_clients` and warns (`mcp_tailscale`) when it is configured but not serving, or when its service starts a Python that's missing or that `brew upgrade` will remove.

Security notes:

- MCP requests need `Authorization: Bearer <token>`; requests without it get 401. `GET /health` is public.
- The token can read and change your reminders. If it was ever committed, synced, pasted into a chat, or shared, run `remctl mcp token --rotate` and save the new token on each device. Apps read it the next time they start or reconnect, so you don't need to add them again.
- On the Mac, the token lives in `~/.config/remctl/mcp-http.json` (mode 0600). If you keep `~/.config` in a dotfiles repository, exclude `remctl/`.
- The server checks the `Origin` and `Host` headers against loopback and your Tailscale identity, which blocks DNS-rebinding attacks from web pages.
- The endpoint has the same power as the CLI, including deletes. Only devices you own should hold the token.
- The endpoint is HTTP on loopback only. Tailscale provides HTTPS and the network boundary. Do not put the port on `0.0.0.0`.

## Tools

Every tool returns `structuredContent` plus the same JSON as a text block, so clients that ignore structured results still get the full output. Lists are wrapped as `{"items": [...], "count": N}`. Reminder rows carry the same numeric `id` the CLI uses.

| Tool | Arguments | Runs |
| --- | --- | --- |
| `today` | `include_overdue` (default true) | `today --json` |
| `upcoming` | `days` 1 to 365 (default 7) | `upcoming N --json` |
| `overdue` | none | `overdue --json` |
| `flagged` | none | `flagged --json` |
| `search` | `query`, `include_completed`, `list` or `list_id`, `limit` (1 to 500, default 100), `offset` | `search … --limit N --offset N --json -- QUERY` |
| `show_list` | `list` or `list_id`, `include_completed` | `show LIST --json` |
| `lists` | none | `lists --json` |
| `get_list` | `list` or `list_id` | `list-info LIST --json` |
| `get_reminder` | `reminder_id`, `include_deleted` (default false) | `info ID [--include-deleted] --json` |
| `resolve_location` | `query` | `location-lookup --json -- QUERY` |
| `create_reminder` | `title`, `list` or `list_id`, `notes`, `due`, `priority`, `recurrence`, `alarm`, `url`, `tags`, `flagged`, and with `private: true`: `grocery`, `section`, `section_id`, `new_section`, `subtasks`, `assign`, `early_reminder`, `urgent`, `location_address` or `latitude` and `longitude`, `location_title`, `radius`, `proximity` | `add … --json -- TITLE` |
| `update_reminder` | `reminder_id` plus one or more of `title`, `list`, `list_id`, `notes`, `due`, `priority`, `recurrence`, `alarm`, `url`, and with `private: true`: `tags`, `set_tags`, `remove_tags`, `clear_tags`, `unassign`, and the `create_reminder` metadata fields | `edit ID … --json` |
| `set_completion` | `reminder_id` or `reminder_ids` (up to 50), `completed`, optional `completion_date` | `done` or `undone ID… --json` |
| `set_flagged` | `reminder_id`, `flagged` | `flag` or `unflag ID --json` |
| `recently_deleted` | `limit` (1 to 500, default 100), `offset` | `deleted --limit N --offset N --json` |
| `restore_reminder` | `reminder_id`, `list` or `list_id`, `private: true` | `restore ID --list LIST --private --json` |
| `delete_reminder` | `reminder_id` or `reminder_ids` (up to 50) | `delete ID… --force --json` |
| `create_list` | `name`, `color`, and with `private: true`: `symbol`, `emoji`, `groceries`, `grocery_locale`, `group` or `group_id` | `list-create … --json -- NAME` |
| `update_list` | `list` or `list_id`, plus `new_name`, and with `private: true`: `color`, `symbol`, `emoji`, `groceries` or `standard`, `grocery_locale` | `list-rename` or `list-edit --private` |
| `doctor` | none | `doctor --for-agent --json` |
| `run` | `args` (exact CLI arguments), optional `stdin` | that command |

Notes:

- `due` accepts `YYYY-MM-DD` (all day), `YYYY-MM-DD HH:MM` (timed), relative forms such as `tomorrow 09:30` or `+3d`, and `clear` in `update_reminder`. A repeating reminder must keep a due date, so `clear` on one is refused with `repeating_reminder_requires_due_date`, unless the same call also clears its `recurrence`.
- A relative `alarm` (`15m`, `1h`, `1d`) counts back from the due date. Without one, the call is refused with `relative_alarm_requires_due_date`; an ISO date works without a due date.
- `completion_date` applies only with `completed: true`.
- `recurrence` needs a due date. It accepts `daily`, `weekly`, `monthly`, `yearly`, an interval such as `daily x2`, weekdays such as `weekly mon,wed,fri`, month days such as `monthly 1,15`, and ordinal weekdays such as `monthly 4th-fri` or `monthly last-fri`. It also accepts a recurrence object exported by RemCTL, as a JSON string of up to 4,096 characters. In `update_reminder`, `clear`, `none`, or `never` removes the repeat rule.
- `run` refuses `mcp`, `onboard`, `setup`, `permissions`, `completion`, and `open` because they are interactive or setup commands, including when top-level options such as `--format json` come first. Include `--json`. Destructive commands need `--force`.
- Values that start with `-`, such as a search for `-urgent`, are passed as values, never as options.
- `delete_reminder` is annotated as destructive so hosts ask for confirmation. Supported accounts retain deleted reminders in Recently Deleted for up to 30 days.
- `recently_deleted` pages by parent and nests subtasks. Use `restoreId` with `restore_reminder`; a destination list in the same account and `private: true` are required. Recovery preserves IDs and verifies the whole hierarchy. An already active ID in that list returns `already_restored` without writing or verifying its former subtasks. For `restore_unconfirmed`, read `get_reminder` with `include_deleted: true` and refresh Recently Deleted before retrying. See [recovery details](commands.md#recently-deleted).
- Integer-like and boolean-like strings are accepted for typed arguments, because widget actions and some models send them as text.
- `create_reminder` reports the new reminder's numeric `id`, the same id `get_reminder`, `update_reminder`, `set_completion`, `set_flagged`, and `delete_reminder` take. The CloudKit identifier that `remctl add --json` calls `id` is reported as `cloudKitId`. When RemCTL cannot read the number back, the result carries a `numeric_id_unavailable` warning instead of an id that cannot be used.
- `priority` accepts the names `high`, `medium`, `low`, and `none`, and Apple's numbers (`0`, `1`-`4`, `5`, `6`-`9`).
- `tags` accepts a list of strings as well as a comma-separated string. Without `private`, `create_reminder` appends them to the title as `#hashtags`; with `private: true` they are synced Reminders tags.
- `private: true` is the typed form of the CLI's `--private`. It is required for synced tags, rich links, sections, subtasks, assignment, Early Reminders, urgent state, and location alarms, which use ReminderKit except for location alarms, which use EventKit. Without it, private-only arguments are refused before anything runs. `url` appends to notes; `create_reminder` can also add tags as title `#hashtags`. `update_reminder` requires `private: true` for tags. See [private-metadata.md](private-metadata.md).
- `subtasks` takes titles. An item can also be a JSON object string, such as `{"title":"Follow up","due":"2026-10-02"}`, with the fields the CLI's `--subtask` accepts.
- `get_list` returns the list plus the section ids that `section_id` needs when two sections share a name, and the sharees that `assign` accepts. Prefer a sharee's address or id to a name.
- `search` covers titles, notes, and saved rich links, ignoring case and accents. It always returns a page: `items`, `count`, `total`, `offset`, `limit`, `hasMore`, and `nextOffset`. While `hasMore` is true, call again with `offset` set to `nextOffset`. When two lists share a name, `list` fails and names their ids; use `list_id`.
- `set_completion` and `delete_reminder` take `reminder_ids` for a batch of up to 50, and always return the batch shape for them, even for one id. The result lists each id under `results`, plus `succeeded`, `failed` (not changed), and `uncertain`. It is an error unless every id succeeded, and the per-id results are kept either way. Do not retry an `uncertain` id without checking it with `get_reminder`: a repeating reminder advances one occurrence per completion, which is also why `set_completion` is not marked idempotent. Repeated ids are written once. A batch stops after a stalled or uncertain write and starts no write after 60 seconds; both tools allow 300 seconds.
- `location_address` is resolved to coordinates before anything is written. RemCTL uses the match only when it is the only one, street-sized, names the street or place typed, and the address gives a town or postal code. Anything else, or a personal label such as `Home`, stops the call with a structured error (`location_not_found`, `location_ambiguous`, `location_imprecise`, `location_unconfirmed`, `location_label_not_address`) and changes nothing. `resolve_location` runs the same lookup without writing; it is the one tool that contacts a service outside the Mac (Apple's geocoder), so it is marked `openWorldHint: true`.
- A successful result larger than 48 kB is cut to fit, because clients drop longer results silently. The largest list keeps its first rows, and the result gains `truncated: true` and `resultBudget` (`totalBytes`, `list`, `totalItems`, `returnedItems`, `hint`). A cut `search` or `recently_deleted` page also gets `count`, `hasMore`, and `nextOffset` for the rows it kept, so following `nextOffset` skips nothing. At least one row is always kept. Errors are never cut.
- `create_list` reports the new list's numeric `id`. `update_list` without `private` can only rename, through EventKit; with `private: true` it renames and changes appearance through ReminderKit.

Every tool descriptor has `title`, `description`, an `inputSchema` with `additionalProperties: false`, an `outputSchema` where the shape is fixed, and annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`, which is true only for `resolve_location`).

Errors use the two channels the specification defines. A malformed request or an unknown tool is a JSON-RPC error (`-32602`). A tool that ran but failed returns `isError: true` with `structuredContent.error`. When RemCTL emitted a structured error on stderr, for example `invalid_due_date` or `confirmation_required`, that object is passed through so the model can correct the call. A write tool that times out returns `timeout` with `outcome: "unknown"`, because the change may already be saved: read the reminder before trying again.

Two prompts are included: `daily_review` and `plan_week`.

## The widget

RemCTL implements the [MCP Apps extension](https://modelcontextprotocol.io/extensions/apps/overview) (`io.modelcontextprotocol/ui`, spec 2026-01-26) with one self-contained page, `ui://remctl/reminders-v1.html`, served as `text/html;profile=mcp-app`. It loads no external resources and makes no network requests.

What it shows:

- **Reminder rows** (`today`, `upcoming`, `overdue`, `flagged`, `search`, `show_list`): check circles, title with flag, priority, and urgent marks, the list name when rows span lists, friendly due dates with overdue in red, a Repeats column when any row recurs, and section headers for `show_list`. Row actions: Mark Done, Reschedule and Edit Title with an inline field, and Delete with a two-step confirmation.
- **Reminder card** (`get_reminder`): notes, tags, link, subtasks, alarms, attachment count, and the same actions.
- **Change confirmation** (`create_reminder`, `update_reminder`, `set_completion`, `set_flagged`, `delete_reminder`, `create_list`, `update_list`): a headline per outcome, the new id, the resolved list, and warnings in amber, for example a created reminder whose flag step failed. A batch shows how many ids changed and lists each id that did not, with its reason.
- **Lists**, **doctor**, and a safe summary for `run`. `run` output shaped like reminder rows gets the rows view.

Widget actions are ordinary `tools/call` requests routed through the host, so the host's approval rules apply. A failed action shows a visible Failed state with the CLI's message; it never resets silently.

Linkage follows the extension's capability rule. A client that advertises `extensions["io.modelcontextprotocol/ui"].mimeTypes` with the Apps MIME type receives `_meta.ui.resourceUri` on tools and results, the resource in `resources/list`, and result hints under `_meta["net.macstories.remctl/ui"]`. MIME types are compared after normalization, so quoting, spacing, and extra parameters are accepted. Legacy clients that did not negotiate Apps receive only the flat `ui/resourceUri` and `openai/*` aliases. Modern clients without Apps receive nothing extra. `resources/read` always includes the sandbox policy.

## Codex plugin mode

The [Codex plugin](desktop-plugin.md) starts the same server with `REMCTL_PLUGIN=1`. In that mode, the server adds 46 tools to the standard 21, for 67 in total, and replaces the reminders widget with the workspace (`ui://remctl/workspace-<hash>.html`) and its settings. Only `open_workspace` is visible to the model; the other plugin tools are called by the workspace itself:

- Workspace: `open_workspace`, `open_selection`, `open_remctl_file`, `workspace_query`, `workspace_detail`, `search_mentions`, `read_settings`, `update_settings`, `workspace_mutate`, `workspace_catalog`, `export_remctl_file`, `review_import`, `import_remctl_file`, `workspace_attach_image`, `choose_reminder_details`, `event_activity`, `preview_smart_filter`, `export_attachment`.
- `manage_*` wrappers for groups, sections, sharees, tags, templates, smart lists, moves, pins, list deletion and renaming, stats, subtasks, and links.

The plugin always launches `$HOME/bin/remctl mcp`. See [custom install locations](desktop-plugin.md#custom-install-locations) if RemCTL lives elsewhere.

## Protocol

The server follows the [2026-07-28 versioning rules](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning) with support for earlier revisions.

**Modern (2026-07-28).** Every request carries `_meta["io.modelcontextprotocol/protocolVersion"]` and `_meta["io.modelcontextprotocol/clientCapabilities"]`. `server/discover` returns the supported versions, capabilities, instructions, and server identity. Every result has `resultType: "complete"` and `_meta["io.modelcontextprotocol/serverInfo"]`. List and read results carry `ttlMs` and `cacheScope`. A request naming another version gets `UnsupportedProtocolVersionError` (`-32022`) with the supported list. A 2026-07-28 request missing one of the two required fields gets `-32602` with the same list. A modern `initialize` is refused with `-32601` and the supported versions.

**Legacy (2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05).** `initialize` echoes a requested version from that list, or answers `2025-11-25`. Results omit the modern fields. A missing resource is `-32002`, as those revisions expect. JSON-RPC batches are accepted.

**stdio.** One JSON-RPC message per line. Requests are served concurrently by a small worker pool, so a slow tool call never blocks `ping`. `notifications/cancelled` terminates the tool's subprocess. The process exits when stdin closes.

**Streamable HTTP.** One POST per message on any path (`/remctl` behind Tailscale, `/` locally). Modern requests must send `MCP-Protocol-Version`, `Mcp-Method`, and, for `tools/call`, `resources/read`, and `prompts/get`, `Mcp-Name`; a missing or mismatched header is `400` with `-32020`. Unknown methods are `404` with `-32601`. Notifications get `202`. Legacy clients get an `Mcp-Session-Id` on `initialize`; an unknown session id is `404`, which tells the client to initialize again. `GET` is `405` because no server-initiated stream is offered. `DELETE` ends a session. `GET /health` needs no token and returns the version and supported protocol versions.

**Events.** The server also advertises the `events` capability, with `events/list`, `events/subscribe`, and `events/unsubscribe` and ten reminder and list events, delivered as signed webhooks. It's implemented and tested, but no client we've tried can subscribe yet, so the Codex workspace hides it. See the [Events notes](notes/events-2026-09-30.md).

Tools are listed in a fixed order to support prompt caching. The server was verified against the official MCP Python SDK 2.2 client in modern and legacy modes over both transports, and against Claude Code and Codex.

## Reference

Commands:

| Command | Purpose |
| --- | --- |
| `remctl mcp` or `remctl mcp serve` | Run the stdio server (what apps launch) |
| `remctl mcp serve --http [--port N]` | Run the HTTP endpoint on loopback (what the LaunchAgent runs) |
| `remctl mcp install [--client …]` | Connect apps; `tailscale` only when named |
| `remctl mcp remove [--client …]` | Disconnect apps or stop the tailnet endpoint |
| `remctl mcp status` | Which apps are connected, and the endpoint state |
| `remctl mcp config [--format …]` | Snippets: `command`, `json`, `toml`, `claude-code`, `codex`, `hermes`, `tailscale` |
| `remctl mcp token [--rotate]` | Print or rotate the endpoint token |
| `remctl mcp bundle [--output PATH] [--open]` | Build the Claude Desktop extension |

All accept `--json`.

Files:

| Path | Purpose |
| --- | --- |
| `~/.config/remctl/mcp-http.json` | Endpoint port, token, and Tailscale identity (mode 0600) |
| `~/Library/LaunchAgents/net.macstories.remctl.mcp-http.plist` | Endpoint service |
| `~/Library/Logs/remctl-mcp-http.log` | Endpoint log |
| `~/Downloads/RemCTL.mcpb` | Default bundle output |

Set `REMCTL_MCP_DEBUG=1` in the client's environment for a per-request trace on stderr.

## Troubleshooting

- **`claude mcp list` says Failed to connect.** Run `remctl mcp config --format command` and execute that command in a terminal. It should wait silently; press Control-D to exit. A traceback means the CLI itself is broken: run `remctl doctor`.
- **Tools work in Claude Code but Claude Desktop shows nothing.** Quit and reopen Claude Desktop after `remctl mcp install --client claude-desktop`, or install the `.mcpb` bundle.
- **`doctor` says the connection points at a different path.** RemCTL moved, for example to a new `PREFIX`. Run `remctl mcp install` again.
- **`doctor` says a connection starts a missing or versioned Python.** The app was registered with a Python path that no longer exists, or with a versioned Homebrew path that `brew upgrade` will remove. Run `remctl mcp install` again. For the tailnet service, run `remctl mcp install --client tailscale`.
- **A tool reports that the Capability Host is unavailable.** Call the `doctor` tool or run `remctl doctor` and follow its fix text. The MCP server never bypasses the host.
- **The tailnet endpoint is configured but not serving.** `remctl mcp status` shows which part is down. `remctl mcp install --client tailscale` repairs the service and the serve mount. `tailscale serve status` lists the mounts.
- **Another device gets 401.** Its saved token differs from the Mac's, usually after a rotation. Print the token with `remctl mcp token` and save it again on that device with `security add-generic-password -U -a remctl -s remctl-mcp-token -w`.
- **An app says its header helper failed.** The Keychain item is missing on that device. `security find-generic-password -s remctl-mcp-token` checks for it; save the token as in step 1.
