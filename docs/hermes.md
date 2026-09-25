# Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is Nous Research's open-source AI agent. It can use tools from MCP servers, and it ships skills for common apps. This page connects RemCTL to Hermes on the Mac where RemCTL is installed.

## RemCTL is not remindctl

Hermes bundles a skill named `apple-reminders`. That skill runs [`remindctl`](https://github.com/steipete/remindctl), a different command-line tool by Peter Steinberger. RemCTL is unrelated to it. The names are close, so check which one a setup refers to.

| | RemCTL | remindctl |
| --- | --- | --- |
| How Hermes uses it | MCP server: `remctl mcp` gives Hermes typed tools | A skill that tells the model which shell commands to run |
| Reminder ids | Stable numeric ids from Reminders' database | Row numbers from the last listing, or EventKit id prefixes |
| Permissions | Held by one signed app, RemCTL Capability Host. Hermes, Terminal, and Python get none. | Granted to whatever app runs `remindctl`, usually the terminal |
| Reads | Reminders' local database: sections, subtasks, tags, rich links, attachments, Early Reminders, display dates | Public EventKit only |
| Writes | EventKit, plus opt-in ReminderKit (`private: true`) for sections, synced tags, rich links, subtasks, assignment, Early Reminders, urgent state, images, groups, smart lists, and templates | Public EventKit only |

[remindctl-comparison-2026-09-26.md](remindctl-comparison-2026-09-26.md) compares the two feature by feature, with sources.

## Connect RemCTL

1. Install and onboard RemCTL on the Mac, if you have not: `./install.sh --bootstrap`, then `remctl onboard`. See [installation.md](installation.md).
2. Print the Hermes entry:

   ```bash
   remctl mcp config --format hermes
   ```

   It looks like this, with this Mac's paths:

   ```yaml
   mcp_servers:
     remctl:
       command: "/opt/homebrew/opt/python@3.14/bin/python3.14"
       args: ["/Users/you/bin/remctl", "mcp"]
       timeout: 300  # RemCTL's batch tools allow up to 300 seconds
   ```

3. Merge it into `mcp_servers` in `~/.hermes/config.yaml`. If the file already has an `mcp_servers:` key, add only the `remctl:` block under it.
4. Start a new Hermes session. Hermes lists the tools as `remctl` tools: `today`, `search`, `create_reminder`, `set_completion`, and the rest in [mcp.md](mcp.md#tools).

The command uses an absolute Python path so it works when Hermes starts with a minimal `PATH`. For a Homebrew Python it is the stable `opt` path, so `brew upgrade` does not break it.

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

Hermes on another machine in your tailnet can use the HTTP endpoint instead. Run `remctl mcp install --client tailscale` on the Mac, then `remctl mcp config --format tailscale`, and add a `url` entry with the bearer header:

```yaml
mcp_servers:
  remctl:
    url: "https://<your-mac>.<tailnet>.ts.net/remctl"
    headers:
      Authorization: "Bearer ${REMCTL_MCP_TOKEN}"
```

Put `REMCTL_MCP_TOKEN=<token>` in `~/.hermes/.env` on that machine.

## Draft catalog entry

Hermes also has a curated MCP catalog in its repository (`optional-mcps/<name>/manifest.yaml`), added only by pull request. This draft follows its manifest format. It has not been submitted.

```yaml
# Nous-approved MCP catalog entry.
# Presence in this directory = approval. Merged via PR review.
manifest_version: 1

name: remctl
description: >-
  Apple Reminders on this Mac: lists, sections, subtasks, tags, due dates,
  location alarms, and batches, through a signed permission host.
source: https://github.com/viticci/remctl

# Local stdio server. RemCTL's installer puts `remctl` in ~/bin; the signed
# RemCTL Capability Host holds every macOS permission, so Hermes needs none.
transport:
  type: stdio
  command: remctl
  args: ["mcp"]

auth:
  type: none

suggest:
  keywords:
    - reminders
    - apple reminders
    - remctl

post_install: |
  RemCTL must be installed on this Mac first:
    git clone https://github.com/viticci/remctl && cd remctl
    ./install.sh --bootstrap
    remctl onboard
  onboard grants Reminders, Automation, and Full Disk Access to RemCTL
  Capability Host only. Never grant them to Hermes, Terminal, or Python.
  If Hermes cannot find `remctl`, replace this entry with the output of
  `remctl mcp config --format hermes`, which uses absolute paths.
```
