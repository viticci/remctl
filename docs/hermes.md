# Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is Nous Research's open-source AI agent. It can connect to RemCTL over MCP on the Mac where RemCTL is installed.

## RemCTL is not remindctl

Hermes bundles a skill named `apple-reminders`. That skill runs [`remindctl`](https://github.com/steipete/remindctl), a different command-line tool by Peter Steinberger. RemCTL is unrelated to it. The names are close, so check which one a setup refers to.

| | RemCTL | remindctl |
| --- | --- | --- |
| How Hermes uses it | MCP server: `remctl mcp` gives Hermes typed tools | A skill that tells the model which shell commands to run |
| Reminder ids | Stable numeric ids from Reminders' database | Row numbers from the last listing, or EventKit id prefixes |
| Permissions | Held by one signed app, RemCTL Capability Host. Hermes, Terminal, and Python get none. | Granted to whatever app runs `remindctl`, usually the terminal |
| Reads | Reminders' local database: sections, subtasks, tags, rich links, attachments, Early Reminders, display dates | Public EventKit only |
| Writes | EventKit, plus opt-in ReminderKit (`private: true`) for sections, synced tags, rich links, subtasks, assignment, Early Reminders, urgent state, the messaging contact, images, groups, smart lists, and templates | Public EventKit only |

[The remindctl comparison](notes/remindctl-comparison-2026-09-26.md) compares the two feature by feature, with sources.

## Connect RemCTL

1. Install RemCTL on the Mac and finish onboarding, if you haven't. See [installation](installation.md).
2. Print the Hermes entry:

   ```bash
   remctl mcp config --format hermes
   ```

   It looks like this, with this Mac's paths:

   ```yaml
   mcp_servers:
     remctl:
       command: "/Library/RemCTL/Python/<content-id>/bin/python3.13"
       args: ["/Users/you/bin/remctl", "mcp"]
       timeout: 300  # RemCTL's batch tools allow up to 300 seconds
   ```

3. Merge it into `mcp_servers` in `~/.hermes/config.yaml`. If the file already has an `mcp_servers:` key, add only the `remctl:` block under it.
4. Run `hermes mcp test remctl` to check the connection. Then start a new Hermes session. Hermes lists the tools as `remctl` tools: `today`, `search`, `create_reminder`, `set_completion`, and the rest in [mcp.md](mcp.md#tools).

The command uses RemCTL's protected Python by absolute path, so it works when Hermes starts with a minimal `PATH`. If a RemCTL upgrade ships a new Python, that path changes: print the entry again and update your Hermes config.

Hermes connects to a stdio server with the older `initialize` handshake by default and falls back to the 2026-07-28 revision. RemCTL answers both, so the default `protocol: auto` works.

Optional settings in the same block:

- `trust: untrusted` makes Hermes ask before every tool call that can change something. RemCTL marks its read tools with `readOnlyHint`, so reads still run without a prompt.
- `tools: {exclude: [run]}` hides the `run` tool, which can execute any RemCTL command, including deletes with `--force`.

## Permissions

RemCTL Capability Host holds Full Disk Access, Reminders, and Automation. Hermes starts `remctl mcp`, and every tool call runs inside the host. Do not grant Reminders, Full Disk Access, or Automation to Hermes, Terminal, or Python. If a tool reports that the host is unavailable, call the `doctor` tool and follow its fix text.

## Using both

The bundled `apple-reminders` skill and the RemCTL tools can both be present. The skill steers the model to `remindctl` shell commands, which need their own Reminders grant on the terminal and return different ids. To keep one path, tell Hermes to use the `remctl` tools for Apple Reminders, or uninstall `remindctl`.

The bundled skill also says to put project task management in GitHub Issues or Notion. Nothing in RemCTL assumes that. Work and project tasks can live in Reminders, for example in a Work list, and RemCTL's list tools target that list by name or numeric id.

## Other devices

Hermes on another machine in your tailnet can use the HTTP endpoint instead. Run `remctl mcp install --client tailscale` on the Mac, then save the token in the other machine's Keychain as described in [mcp.md](mcp.md#1-save-the-token-on-the-other-mac).

Hermes fills `${REMCTL_MCP_TOKEN}` from its `secrets.command` helper, which reads the token from the Keychain when Hermes starts. `remctl mcp config --format tailscale` prints this block with your Mac's URL:

```yaml
secrets:
  command:
    enabled: true
    command: 't=$(/usr/bin/security find-generic-password -s remctl-mcp-token -w) && printf "REMCTL_MCP_TOKEN=%s\n" "$t"'
mcp_servers:
  remctl:
    url: "https://<your-mac>.<tailnet>.ts.net/remctl"
    headers:
      Authorization: "Bearer ${REMCTL_MCP_TOKEN}"
```

Hermes runs one `secrets.command`. If your config already has one, append the RemCTL part to it after a `;`, so the helper prints both lines. Then run `hermes mcp test remctl`; the first line should say `Command helper: applied` and count the RemCTL secret.

On Linux, replace the Keychain read with `secret-tool lookup service remctl-mcp-token`. If you'd rather use `~/.hermes/.env`, Hermes's own secrets file, keep it at mode 0600 and out of any folder you sync or commit.
