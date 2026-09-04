# RemCTL: The Power-User Reminders CLI

![RemCTL](https://cdn.macstories.net/images/uploads/2026/05/26/cleanshot-2026-05-26-at-1629152x-1779805785287-9271e938c2.png)

RemCTL is a fast, scriptable Reminders CLI for macOS designed for power users and AI agents.

Normal installations route permission-bearing commands through one persistent, signed `RemCTL Capability Host.app`. The host reads the user's local iCloud Reminders database for speed and detail, then writes through Apple's public EventKit APIs so changes sync normally to other devices.

RemCTL also offers an optional integration with Reminders' private API on macOS. This allows RemCTL to write proprietary metadata such as sections, shared-list assignments, subtasks, tags, image attachments, urgent state, Early Reminders, display ordering, list appearance metadata, Groceries list metadata, list groups, custom smart lists, and Reminders templates by using the native ReminderKit framework. Location-based alarms are guarded by the same `--private` command surface, but are saved through the public EventKit bridge because that path materializes reliably on current macOS.

This gives scripts and agents access to the modern Reminders data model without writing directly to the sync database. RemCTL installs on macOS 14 or later. The 1.8.0 signed-host release is verified on the current early macOS 27 Golden Gate build. The underlying command and private-API paths also have historical test coverage on macOS 26 Tahoe, but this exact host release has not been rerun there. Private ReminderKit behavior can change between macOS releases.

## How It Works

```text
Terminal / Hermes / Codex / other callers
  -> remctl client (Python 3.10+)
     -> 6 setup and display commands stay in the caller
     -> auto: owner-only Unix socket, broker protocol v2
        -> signed persistent RemCTL Capability Host.app (protected Python 3.13+)
           reads:   SQLite with Full Disk Access
           writes:  sealed remctl-bridge -> EventKit with Reminders access
           flags:   AppleScript with Automation access
           private: sealed remctl-private -> ReminderKit (--private only)
```

Why this architecture exists:

- **Direct SQLite reads** expose sections, subtasks, tags, attachments (with sha512-verified local file paths), deep links, list colors and badges, recurrence metadata, normal alarms, location alarms, and Early Reminder metadata in tens of milliseconds.
- **One permission target** keeps Reminders, Automation, and Full Disk Access grants on the signed capability host. Terminal, Hermes, Codex, Python, and other callers do not need separate RemCTL grants in normal hosted use.
- **Complete protected execution** routes 49 permission-bearing commands through the protocol-v2 broker. Six setup and display commands stay local: `completion`, `doctor`, `list-symbols`, `onboard`, `permissions`, and `setup`.
- **Explicit execution modes** use `REMCTL_CAPABILITY_HOST=auto|force|direct`. `auto` is the default and uses the host when installed. `force` requires it. `direct` bypasses it for diagnostics and degraded recovery, so the caller must have its own access. Setting `REMCTL_STORE_DIR` forces direct execution in `auto` or `direct` mode and is an error with `force`. `REMCTL_BRIDGE_PATH` and `REMCTL_PRIVATE_PATH` affect direct execution only; hosted commands use the sealed helpers fixed by the signed generation.
- **Limited EventKit reads** are available only with `--via-eventkit` on `show`, `search`, `today`, and `upcoming`. The flag never changes the execution route: the signed host performs the EventKit read in normal `auto` mode, while the caller performs it in `direct` mode or with a custom store. It is never selected automatically and does not return RemCTL numeric IDs.
- **EventKit writes** keep Reminders and iCloud in charge of mutations. RemCTL does not write directly to the database.
- **Private metadata writes** are unsupported and explicitly opt-in with `--private`. They use Apple's private ReminderKit APIs, not direct SQLite mutation, and should be treated as experimental power-user functionality.

## Quick Start

The easiest experience is to ask your agent (Claude Code or Codex) to set up RemCTL for you by pointing it at this repo. Alternatively:

```bash
git clone https://github.com/viticci/remctl.git
cd remctl
./install.sh --bootstrap
~/bin/remctl onboard
```

Pause here if onboarding opens the Full Disk Access helper. Add only the exact signed `RemCTL Capability Host.app` shown there. If you changed its Full Disk Access authorization, restart the host:

```bash
launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
```

After any required restart, verify the installation and try a normal command:

```bash
~/bin/remctl doctor
~/bin/remctl today
```

`install.sh` builds, signs, and strictly verifies a complete capability-host generation before it transactionally replaces and starts the LaunchAgent service. The command-line client supports Python 3.10 or newer. The signed host uses a separate protected Python 3.13+ selected by the installer. Xcode Command Line Tools, an official python.org Framework installation of Python 3.13 or newer, and an Apple Development signing identity with a TeamIdentifier are the supported tester setup. The installer preserves an existing identity, accepts a valid explicit `REMCTL_CODESIGN_IDENTITY`, or auto-detects an Apple Development identity. A Mac without that identity is not currently eligible for a live Capability Host install; RemCTL does not fall back to ad-hoc signing. `--bootstrap` also creates RemCTL's config directory; shell completion is installed when supported. For zsh, `setup` prints the `fpath` lines to add to `~/.zshrc` when your config does not already load the completion directory.

`remctl onboard` asks the signed host to present the native Reminders and Automation consent flows. macOS has no native Full Disk Access prompt, so onboarding opens the guided helper only when that grant is missing. If it does, add only the exact signed host shown by the helper, then restart it with `launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"` before running `doctor`. Run `remctl permissions full-disk-access` only when you need to reopen that guide or repair the grant.

When no onboarding state exists, the first interactive, non-JSON permission-bearing invocation runs onboarding before the requested command. RemCTL skips this automatic flow for local setup/display commands, non-TTY or JSON calls, host-internal execution, and `REMCTL_SKIP_ONBOARD=1`. It prints the checks and then continues with the original command; warnings do not block that command, and any unresolved permission failure surfaces from the command itself.

The installer also creates two aliases in your bin directory — `rctl` and `reminders` — that behave identically to `remctl`. Any command in this README works with any of the three names.

If the installer says `PATH action required`, add the printed PATH line and open a new Terminal window before typing `remctl`, `rctl`, or `reminders`.

For a first install to `~/.local/bin`, use:

```bash
PREFIX="$HOME/.local" ./install.sh --bootstrap
```

Keep using the same prefix for upgrades, but omit `--bootstrap`: `PREFIX="$HOME/.local" ./install.sh`.

The supported legacy migration recognizes the exact public files from the official 1.7.1 release, the expected aliases, and the types of its compiler-generated native helpers. Start with a normal upgrade. When those generated helpers are present, the installer still requires a manual review before the one-time `--adopt-existing-install` run. Because 1.7.1 predates the signed Capability Host, the first 1.8.0 install is also a first host install: after installation, run `remctl onboard`, complete any Full Disk Access step and host restart, then run `remctl doctor --for-agent --json`. It cannot preserve host grants that did not exist in 1.7.1.

For either an exact official 1.7.1 install with generated helpers or an expected prerelease host install, first review every existing RemCTL path named by the refusal. Confirm it belongs to that expected installation. For a prerelease host, also confirm the signed app and LaunchAgent. Then rerun once with the same prefix and `--adopt-existing-install`, for example `./install.sh --adopt-existing-install`. Move modified, unknown, or foreign paths aside; never adopt them. After a prerelease-host adoption, run onboarding only if this Mac has not already granted access to that exact signed host identity. Normal later upgrades use `./install.sh` and verify exact hashes from `.remctl-install-manifest.json`.

Full setup details live in [docs/installation.md](docs/installation.md). Release notes live in [CHANGELOG.md](CHANGELOG.md).

## Uninstalling

To remove RemCTL files installed by `install.sh`, run:

```bash
./uninstall.sh
```

The guarded, identity-checked uninstaller checks `~/bin` and `~/.local/bin` by default, or the destinations selected by `PREFIX`, `REMCTL_BIN_DIR`, `REMCTL_APP_DIR`, and `REMCTL_LAUNCH_AGENT_DIR`. It stops the exact RemCTL LaunchAgent, unregisters and removes the exact signed host app, removes its private socket and known CLI files, removes `completions` only when empty, and supports `--dry-run` and `--keep-config`. It does not edit shell config or revoke macOS privacy permissions.

## Command Map

| Task | Commands |
| --- | --- |
| See what is due | `today`, `upcoming`, `overdue` |
| Browse reminders | `lists`, `groups`, `group-info`, `smart-lists`, `templates`, `template-info`, `show`, `search`, `flagged`, `urgent`, `info`, `subtasks`, `sharees` |
| Create and edit | `add`, `edit`, `reminder-move`, `done`, `undone`, `delete`, `flag`, `unflag` |
| Organize | `list-symbols`, `list-create`, `list-edit`, `list-pin`, `list-unpin`, `list-rename`, `list-delete`, `section-create`, `section-rename`, `section-delete`, `group-create`, `group-edit`, `group-delete`, `smart-list-create`, `smart-list-edit`, `smart-list-delete`, `template-create`, `template-apply`, `template-delete`, `sections`, `tags` |
| Share data | `export`, `import`, `link`, `open`, `--json`, `--format table` on tabular read commands |
| Set up the Mac | `onboard`, `permissions`, `doctor`, `setup`, `completion` |

Common examples:

```bash
remctl today
remctl groups
remctl show Work --format table
remctl show --list-id 153 --json
remctl today --via-eventkit --json
remctl show Work --via-eventkit
remctl add "Review PR" -l Work -d "tomorrow 10:00" -p high
remctl add "Pay rent" -d "2026-06-01" --recurrence monthly
remctl add "Team sync" -l Work -d "2026-08-28 10:00" --recurrence "monthly x2 4th-fri"
remctl done 23880 --date "2026-05-27 09:30"
remctl edit 23880 -d clear
remctl edit 23880 -l Work
remctl reminder-move 23880 --before 23881 --private
remctl reminder-move 23880 --last --smart-list "Focus" --private
remctl edit 23880 --private --set-tags remctl,work
remctl section-create "Research" -l Projects --private
remctl section-rename "Research" --new-name "Reading" -l Projects --private
remctl add "Research" -l Projects --private --url "https://example.com" -t remctl --new-section "Research"
remctl sharees Shopping --json
remctl add "Pick up groceries" -l Shopping --private --assign Alex
remctl add "Leave early" -l Work -d "today 14:00" --private --early-reminder 15m
remctl add "Launch assets" -l Projects --private --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"]}'
remctl list-symbols
remctl list-symbols --preview
remctl list-create "Research" --color orange --private --symbol education3
remctl list-create "Cold Ideas" --color cyan --private --emoji 🥶
remctl list-create "Groceries" --private --groceries --grocery-locale en_US
remctl list-create "Ideas" --private --group Writing
remctl group-info "Writing" --json
remctl group-create "Writing" --private --add-list Editorial
remctl group-edit "Writing" --private --new-name "Drafts" --add-list Ideas
remctl group-edit "Writing" --private --move-list Ideas --before-list Editorial
remctl group-delete "Drafts" --private --force
remctl add "Milk" -l Groceries --private --grocery
remctl smart-lists --json
remctl smart-list-create "Flagged Review" --private --flagged
remctl smart-list-create "Priority or Today" --private --match any --priority high,medium --date today
remctl smart-list-create "Due Before June 1" --private --date-range 2026-05-16,2026-05-31 --color red --emoji 📆
remctl smart-list-edit "Priority or Today" --private --priority high
remctl smart-list-delete "Flagged Review" --private --force
remctl templates --json
remctl template-info "Rome: Things To See" --json
remctl template-create "Packing Template" --from-list Packing --private --json
remctl template-apply "Packing Template" --private --json
remctl template-delete "Packing Template" --private --force
remctl list-edit Projects --private --color orange --symbol education3
remctl list-pin "Project X" --private
remctl list-rename --list-id 123 --new-name "Project X Archive"
remctl info 23880 --json
```

The full command guide is in [docs/commands.md](docs/commands.md). For smart lists specifically, start with [Smart Lists in the command guide](docs/commands.md#smart-lists), then read [Private Metadata Writes: Smart List Examples](docs/private-metadata.md#smart-list-examples) for the ReminderKit write path, guardrails, and implementation notes. Template commands are covered in [docs/commands.md#templates](docs/commands.md#templates) and [docs/private-metadata.md#template-examples](docs/private-metadata.md#template-examples).

`import` is a convenience importer for ordinary reminder fields, not a lossless restore operation. It accepts a JSON array from a file or `-` for standard input and supports `title`, `list`, `notes`, `due`/`dueDate`, `priority`, `url`, `recurrence`, `alarm`, and Boolean `flagged`. It validates the entire document before writing. Runtime failures can still produce a partial import: successful reminders remain, the command exits nonzero, and the final JSON summary reports created IDs and failed indexes. See [Import and Export](docs/commands.md#import-and-export) before using an export as import input.

`--via-eventkit` is a limited read-only fallback, not an alternate primary mode. It works only for `show`, `search`, `today`, and `upcoming` during explicit diagnostics or degraded recovery. It does not change routing and is never selected automatically: normal `auto` execution sends the command to the signed host, which performs the EventKit read; `direct` mode and `REMCTL_STORE_DIR` run it in the caller. JSON output is a wrapper object with `source: "eventkit"`, `fidelity: "limited"`, and `items`; each item has `eventKitId`, not RemCTL's numeric `id`. Never pass `eventKitId` to `info`, `edit`, `done`, `delete`, `link`, `open`, `subtasks`, or any command that expects a numeric RemCTL ID. This mode also cannot show sections, synced tags, private rich links, urgent state, template internals, smart-list internals, numeric list IDs, or table output.

Due dates are atomic. If `-d/--due` is present and RemCTL cannot parse it, the command fails before creating or editing anything. Supported deterministic forms include `YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, `today at 3pm`, `tomorrow 09:30`, `tonight at 11`, `Friday at 15:00`, `next friday at 3pm`, `+3d`, `eod`, and `eow`. In create mode, date-only forms such as `today`, `tomorrow`, `YYYY-MM-DD`, `+3d`, and `next friday` create all-day reminders; forms with explicit times create timed reminders.

Recurrence, normal alarm, and priority inputs are also validated before writes. Supported recurrence forms are `daily`, `weekly`, `monthly`, `yearly`, `weekly mon,wed,fri`, and `monthly 1,15`. An optional `xN` interval token (1–999) follows the frequency: `daily x2`, `weekly x2 thu`, `monthly x3 15`, `yearly x2`. Monthly rules can also pin a weekday to a week of the month: `monthly 4th-fri`, `monthly 1st-mon,3rd-mon`, `monthly last-fri` (alias of `-1-fri`), down to `-5-fri`. Ordinal suffixes are checked, so `4st-fri` is rejected; week-pinned days cannot be mixed with plain day-of-month numbers, and they are monthly-only. Use `last-fri` rather than `5th-fri` for "the last Friday": EventKit skips months that have no fifth Friday. `upcoming DAYS` requires a positive range from 1 to 3650 days.

## Assignees

Assignment is for shared Reminders lists only and requires `--private`. You do not need a person's email if their name is unique in the shared list, but email/phone address or ID is safer for scripts and agents.

```bash
remctl sharees Shopping
remctl sharees Shopping --json
remctl add "Pick up groceries" -l Shopping --private --assign Alex
remctl edit 23880 --private --assign alex@example.com
remctl edit 23880 --private --assign me
remctl edit 23880 --private --unassign
```

`--assign USER` resolves `USER` against the target list's sharees by display name, first/last name, email or phone address, numeric sharee ID, object UUID, or `me`. Names are convenient for humans; agents should call `remctl sharees LIST --json` first and prefer the returned `address`, numeric `id`, or `objectUUID` when duplicate names are possible. `--unassign` clears the current assignment. Verify the result with `remctl info ID --json`; assignment data appears under `assignment`.

## List Groups

Reminders stores list groups as list rows with `listType: "group"` and child lists linked by parent-list columns. `remctl groups` shows only groups with active/completed/total reminder counts, while `remctl lists --json` includes group rows with `children` and child list rows with `group` metadata. `remctl group-info <group>` prints the group ID, object UUID, child lists, counts, and suggested follow-up commands. `remctl show <group>` reads reminders from the group's child lists. In table mode, group output is split by child list and section; with `--completed`, the date column shows completion timestamps instead of due status.

Group writes use private ReminderKit and require `--private`. `group-create` creates a group and can immediately move existing lists into it. `list-create --private --group <group>` creates a new list and assigns it to the group. `group-edit` renames a group, adds/removes child lists, and can reorder a child list with `--move-list` plus `--before-list`, `--after-list`, `--first`, or `--last`. `group-delete` first moves child lists back to the top level before deleting the empty group. These operations change list containers only; reminders stay in their lists.

## Groceries Lists

Reminders stores Groceries lists as normal lists with private grocery metadata. RemCTL reads those fields directly: `lists --json` reports `listType`, `isGroceries`, and the grocery locale flags, while human `lists` and `show` output mark detected Groceries lists with `🥕`. When `show` prints Groceries sections, known Reminders grocery categories get matching leading emoji such as `🥛 Dairy, Eggs & Cheese`, `🥬 Produce`, and `🧻 Household Items`; `show --json` includes `sectionEmoji` for the same categories.

```bash
remctl lists --json
remctl show Groceries
remctl list-create "Groceries" --private --groceries --grocery-locale en_US
remctl list-edit "Shopping" --private --groceries --grocery-locale it_IT
remctl list-edit "Shopping" --private --standard
remctl add "Milk" -l Groceries --private --grocery
remctl edit 23880 --private --grocery
```

Groceries writes require `--private` because Apple exposes the list type, locale, and item categorization through ReminderKit, not EventKit. `add --private --grocery` creates the reminder normally, waits for Reminders' automatic grocery sorter, verifies the resulting section membership from the local database, and only falls back to ReminderKit's explicit categorizer if the item is not sorted yet.

## Smart Lists

RemCTL can inspect built-in and custom smart lists with `smart-lists`. It can also create, edit, and delete custom smart lists with the Reminders.app filters that currently materialize reliably through the private ReminderKit write path: any tag, selected tags, date, time, priority, flag, vehicle-connected, specific location, one included list, and `--match all|any` across those reliable families.

```bash
remctl smart-lists --json
remctl smart-list-create "Any Tag" --private --any-tag
remctl smart-list-create "#remctl Today" --private --tags remctl --date today
remctl smart-list-create "Projects Today" --private --include-list Projects --date today --date-today-include-past-due
remctl smart-list-create "Priority or Today" --private --match any --priority high,medium --date today
remctl smart-list-create "Due Before June 1" --private --date-range 2026-05-16,2026-05-31 --color red --emoji 📆
remctl smart-list-edit --smart-list-id 170 --private --priority high
remctl smart-list-delete "Priority or Today" --private --force
```

Smart-list writes are private ReminderKit writes and always require `--private`; RemCTL rejects unknown filter shapes and known zero-filter shapes before saving. Smart lists support the same private appearance flags as lists: `--color`, `--symbol`, and `--emoji`. Reminders.app currently materializes only one included-list filter at a time. Do not create multi-list aggregate smart lists through a list filter; use one included list, or a different reliable filter family. Use:

- [docs/commands.md#smart-lists](docs/commands.md#smart-lists) for command syntax and supported filters.
- [docs/private-metadata.md#smart-list-examples](docs/private-metadata.md#smart-list-examples) for private API behavior, safety notes, and reverse-engineered filter storage details.
- [SKILL.md](SKILL.md) for the concise agent contract.

## Templates

Reminders templates are saved lists with saved reminders inside them. RemCTL reads them from the local template tables and can create, apply, and delete templates through private ReminderKit APIs. Template support is intentionally list-level: RemCTL can save an entire source list as a template and apply a template to create a new list. It does not append individual reminders to existing templates or strip subtasks/due dates while saving. Existing public template links are reported as read-only metadata; RemCTL does not create iCloud sharing links.

```bash
remctl templates --json
remctl template-info "Rome: Things To See" --json
remctl template-create "Packing Template" --from-list Packing --private --json
remctl template-create "Archive Template" --from-list-id 144 --include-completed --private
remctl template-apply "Packing Template" --private --json
remctl template-delete "Packing Template" --private --force
```

`template-create`, `template-apply`, and `template-delete` require `--private`. `template-create` takes one source list; `--include-completed` is the only content-selection flag. Verify template writes with `templates --json` and `template-info`; verify applied templates with `lists --json` and `show <new list> --json`.

## Private API Features

RemCTL's default writes use EventKit. For metadata Apple does not expose publicly, RemCTL has an explicit `--private` mode backed by `remctl-private`, an Objective-C helper that uses Apple's private ReminderKit framework and saves through the Reminders stack. In normal installed execution, the signed host runs sealed copies of both helpers. RemCTL *never* writes directly to SQLite (which would break iCloud sync and cause database corruption issues).

Private writes are opt-in and power-user only:

```bash
remctl add "Research" -l Projects --private --url "https://example.com" -t remctl --section "Research"
remctl edit 23880 --private --section-id DCD255E2-7CF5-4B45-9566-3F9A5D84AFA8
remctl edit 23880 --private --assign Alex
remctl edit 23880 --private --unassign
remctl add "Launch assets" -l Projects --private --subtask '{"title":"Export PNG","notes":"Use final crop","due":"tomorrow","url":"https://example.com","tags":["media"]}'
remctl add "Leave now" -l Work --private --urgent
remctl add "Leave early" -l Work -d "today 14:00" --private --early-reminder 15m
remctl reminder-move 23880 --before 23881 --private
remctl reminder-move 23880 --last --smart-list "Focus" --private
remctl edit 23880 --private --early-reminder 1h
remctl edit 23880 --private --early-reminder clear
remctl edit 23880 --private --image ~/Desktop/mockup.png --flagged --urgent
remctl edit 23880 --private --location-title "Apple Park" --latitude 37.3349 --longitude -122.0090 --radius 200
remctl list-edit Projects --private --color '#FF8D28' --symbol education3
remctl list-edit Projects --private --emoji 📌
remctl list-create "Groceries" --private --groceries --grocery-locale en_US
remctl add "Milk" -l Groceries --private --grocery
remctl list-pin "Project X" --private
remctl list-pin "Flagged" --private
remctl list-unpin --list-id 144 --private
remctl list-unpin --smart-list-id 4 --private
remctl group-create "Writing" --private --add-list Editorial
remctl group-edit "Writing" --private --new-name "Drafts" --add-list Ideas --remove-list Socials
remctl group-edit "Writing" --private --move-list Ideas --last
remctl group-delete "Drafts" --private --force
remctl smart-list-create "Flagged Review" --private --flagged
remctl smart-list-create "Priority or Today" --private --match any --priority high,medium --date today
remctl smart-list-create "Projects Today" --private --include-list Projects --date today --date-today-include-past-due
remctl smart-list-create "Near Home" --private --location-title Home --latitude 41.9 --longitude 12.5 --proximity enter
remctl smart-list-edit "Priority or Today" --private --priority high --color red --emoji 📆
remctl smart-list-delete "Flagged Review" --private --force
remctl template-create "Packing Template" --from-list Packing --private --json
remctl template-apply "Packing Template" --private --json
remctl template-delete "Packing Template" --private --force
```

Private mode covers the parts of Reminders that EventKit does not expose:

- Reminder metadata: synced web rich links, synced tags, sections, shared-list assignments, rich subtasks, image attachments, real flag state, urgent state, Early Reminders, and location alarms.
- Reminder ordering: move within an ordinary list or an unsectioned, manually ordered custom smart list with `reminder-move --private`.
- List metadata: exact `#RRGGBB` colors, official list symbols, emoji badges, Groceries list conversion/locale metadata, regular-list and custom-smart-list pin state, and list group create/edit/delete. Built-in smart-list pinning is available only when the host macOS exposes the required generic ReminderKit fetch; unsupported systems fail before a save.
- Smart lists: custom smart-list create/edit/delete for the Reminders filters that RemCTL has verified to materialize correctly.
- Templates: whole-list template create/apply/delete. Existing public template links can be read, but RemCTL does not create iCloud sharing links.

A few rules keep this safe and predictable:

- `edit -l/--list` and `edit --list-id` use EventKit first. If a pure move is rejected by a list/container boundary, RemCTL uses a verified ReminderKit clone-delete fallback and returns `oldId` plus the new `id`; move first, then apply unrelated edits to the returned ID.
- `reminder-move` changes display order without changing the reminder's base list. Use `--smart-list` or `--smart-list-id` for an unsectioned custom smart list; sectioned smart-list ordering is refused before saving.
- `show <list>` follows the stored Reminders manual display order in JSON, plain, and table output. `show <group>` applies the same ordering within each child list. A reminder that has not merged into the ordering record yet remains visible after positioned reminders.
- Every delete command requires `--force` under `--json` or when stdin is not interactive. Without it, RemCTL emits `confirmation_required` on stderr and performs no write.
- Shared-list assignment uses `--private --assign USER`; `USER` may be a unique name, email/phone address, numeric sharee ID, object UUID, or `me`. Use `remctl sharees LIST --json` before assigning when scripting.
- Location alarms still require the `--private` guardrail, but RemCTL saves them through `remctl-bridge` because EventKit structured-location alarms persist correctly on current macOS.
- `--private --url` and rich subtask URLs must be public `http` or `https` hosts. Loopback, `.local`, private, link-local, multicast, reserved, and unresolved hosts are rejected before writing.
- Rich-link and image edit operations are additive. RemCTL can add them, but it does not remove or replace existing rich links/images.
- `--early-reminder` accepts values such as `15m`, `1h`, `2d`, `1w`, `1mo`, or `clear`. Non-clear values require a due date.
- `list-create --color` uses public EventKit for normal color names. Add `--private` for exact colors, official symbols, or emoji badges.
- `list-symbols` prints the 71 official Reminders emblem names. Use `list-symbols --preview` for the native badge contact sheet. Use `--emoji` for custom emoji badges.
- Groceries writes verify Reminders' automatic sorter first, then use the private categorizer only if needed.
- Group writes move list containers, not reminders. `group-delete` detaches child lists before deleting the group so their reminders are preserved.
- If a section name is duplicated in the same list, RemCTL uses the single non-empty match when possible. Otherwise, pass `--section-id`.

Verify with:

- `remctl info ID --json` for reminder metadata.
- `remctl sharees LIST --json` before assignment, then `remctl info ID --json` for the resulting `assignment`.
- `lists --json` for list `color`, `badge`, `badgeEmblem`, and Groceries metadata.
- `smart-lists --json` for smart-list filters and pin state.
- `templates --json` or `template-info` for templates.

This is the major difference from ordinary EventKit-only Reminders CLIs, but it is still unsupported by Apple:

- Private-only flags fail before writing unless `--private` is present.
- Generic file/PDF attachments are intentionally rejected.
- Smart-list and template writes should get a UI/device check when sync behavior matters.

## Output

RemCTL output is designed for both humans and agents:

- reminder IDs are shown as `#ID`
- normal JSON read commands return RemCTL numeric `id` values that can be used with `info`, `edit`, `done`, `delete`, `link`, `open`, and `subtasks`
- `--via-eventkit` JSON returns `eventKitId` values instead; they are EventKit calendar item identifiers and cannot be chained into numeric-ID commands
- `--via-eventkit` JSON includes `source: "eventkit"`, `fidelity: "limited"`, and `idWarning` so automation can reject accidental ID chaining
- `#ID` is colored with the reminder list color when RemCTL can read list colors
- flagged reminders show `⚑`
- macOS 26 urgent reminders show `⏰`
- `info --json` reports the actual due date as `dueDate`; if Reminders stores a separate display/alert date, it appears as `displayDate`
- `add -d` creates all-day reminders when the input names a date without a time, such as `today`, `tomorrow`, `2026-06-01`, `+3d`, or `next friday`
- `edit -d` carries a single absolute alarm forward when it matches the old due/display time, keeping Reminders.app's visible time aligned for ordinary reschedules
- `edit -d clear` removes a single matching absolute alarm/display time; `edit --alarm clear` removes normal alarms explicitly
- normal EventKit alarms and location alarms appear in `info --json` as `alarms`
- image attachments appear in `info --json` and list-command JSON as `attachments` entries with `filename`, `type`, `path`, `resolved`, `uti`, `width`, and `height`; `path` is the sha512-verified local file as resolved by the executing process, or `null` with `resolved: false` when the attachment is not downloaded on this Mac
- shared-list assignments appear in human output as `@Name` and in `info --json` as `assignment`
- Early Reminders appear in `info` output (text and JSON) as labels such as `15 minutes before`
- recurring reminders show a repeat badge such as `↻ weekly Mon, Wed`, `↻ monthly 4th Fri`, or `↻ every 2 months 4th Tue`
- Groceries lists show `🥕` in list headings and list summaries
- table output keeps a dedicated `Repeat` column when any row is recurring
- human output strips terminal control characters from Reminders text before printing
- every read command supports JSON output

```bash
remctl today --json
remctl --format table upcoming 14
NO_COLOR=1 remctl today
```

## Inline Images

Reminders with image attachments can render them right in the terminal:

```bash
remctl info 847 --images
remctl show Projects --images --verbose
remctl today --images --verbose --image-mode halfblock --image-width 48
```

`info --images` renders every image attachment inline. List commands (`show`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `search`) render attachments too, but only with `--verbose` so ordinary list output stays compact. Rendering happens only on a real TTY — never in pipes, `--json`, or table mode — so scripts are never surprised by escape sequences.

RemCTL picks the best protocol for the current terminal automatically: the Kitty graphics protocol on Ghostty, Kitty, WezTerm, and Konsole; iTerm2's inline image protocol on iTerm2 (and Blink on iOS over SSH); a truecolor half-block renderer elsewhere. Terminals with no usable protocol simply skip inline rendering — the plain attachment filename lines always remain. Override detection with `--image-mode kitty|iterm2|halfblock|none` and set the render width in cells with `--image-width N` (default: ~40% of the terminal width, capped 24–100; half-block caps at 64). The matching environment variables are `REMCTL_IMAGES=1`, `REMCTL_IMAGE_MODE`, and `REMCTL_IMAGE_WIDTH`; flags win over env.

There is nothing new to install. Rendering uses Pillow if it is importable, otherwise macOS `sips` plus a small stdlib BMP decoder — either path works on a stock Mac.

For agents, the same attachments are machine-readable instead: `info --json` and list-command JSON (`show`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `search`) include an `attachments` array on any reminder that has them. Each entry carries `filename`, `type`, `path`, `resolved`, `uti`, `width`, and `height`. `path` is the sha512-verified on-disk file under Reminders' group container as seen by the signed host. It is not a guarantee that a caller without Full Disk Access can open that protected path itself. A legacy attachment that was never downloaded to this Mac reports `path: null` with `resolved: false`.

## List Badges

Plain human list output ends each reminder line with one or two trailing emoji badges when they apply: `🔗` means the reminder has at least one rich link, `🌄` means at least one image attachment. When both apply they print in that order, space-separated, after tags and any `[N subtasks]` count. Completed reminders keep their badges. The badges appear in `show`, `search`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `group show`, and on subtask lines in `subtasks`/`info` — plain human output only. They are never in `--json`, CSV export, `--format table`, or `--via-eventkit` payloads; agents should read the `attachments` and `url` JSON fields instead.

```text
[ ] #30165 Try Halo app on iOS again 🌄
[ ] #30174 Read the new MacStories piece 🔗
[ ] #30180 Review layout mockups 🔗 🌄
```

## macOS Permissions

The installed `RemCTL Capability Host.app` is the single macOS privacy target for three grants:

- Reminders access for EventKit writes
- Automation access for AppleScript operations, including `flag`, `unflag`, and `add --flag`, which have no EventKit path
- Full Disk Access for database and attachment reads

Run:

```bash
remctl onboard
```

Pause here if onboarding opens the Full Disk Access helper. Add only the signed `RemCTL Capability Host.app`. Do not add Hermes, Python, Codex, Terminal, or another caller for normal hosted access. If you changed the host's Full Disk Access authorization, restart it:

```bash
launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"
```

After any required restart, verify the host:

```bash
remctl doctor
```

`remctl onboard` asks the signed host to present the native Reminders and Automation consent flows. It opens the visual Full Disk Access helper only when that grant is missing. If the helper opens, it copies the exact host app path and shows that app as the draggable target. Use `remctl permissions full-disk-access` only to reopen the guide or repair that grant.

For agent setup, run `remctl doctor --for-agent --json`. The `access.effective` object is authoritative. Direct caller access can remain blocked while `access.effective.route` is `capabilityHost` and `access.effective.ready` is true.

Automation status is retained by the running persistent host, not stored as a new macOS grant. After that host has observed a definitive `authorized`, `denied`, or `notDetermined` result, a transient `targetNotRunning` result keeps the last verified state while the host retries. A cold or restarted host with no definitive in-memory result is conservative: if Reminders cannot run, Automation can report `targetNotRunning` or `unknown` and `fullReady` remains false until the host verifies the state. Reminders.app does not need to stay open between commands after a warm verification.

Manual fallback: run `remctl doctor --for-agent`, then add the printed `RemCTL Capability Host.app` path in System Settings > Privacy & Security > Full Disk Access. In the file picker, press `Command-Shift-G`, paste the path, press Return, then click Open. Restart the host with `launchctl kickstart -k "gui/$(id -u)/net.macstories.remctl.capability-host"`, then rerun `remctl doctor --for-agent`. If Reminders or Automation access is missing, rerun `remctl onboard`; the signed host presents those prompts.

If Full Disk Access cannot yet be granted to the signed host, `show`, `search`, `today`, and `upcoming` support `--via-eventkit` as a limited read-only recovery path through EventKit. It stays hosted in normal `auto` mode and runs in the caller only in `direct` mode or with `REMCTL_STORE_DIR`; RemCTL never selects it automatically. This does not replace normal setup: it omits RemCTL numeric IDs, sections, synced tags, private metadata, smart-list/template internals, numeric list targeting, and table output.

### Renamed iCloud accounts

RemCTL writes only to iCloud Reminders lists. EventKit reports a source's title rather
than its provider, so an iCloud account renamed in System Settings → Internet Accounts
is indistinguishable from a third-party CalDAV server by name alone.

RemCTL resolves this by reading each account's provider from the macOS accounts
database, which needs no permission beyond the Full Disk Access it already requires for
the Reminders store. Renamed accounts work normally, with nothing to configure.

If that lookup is unavailable, writes to the renamed account are refused with an error
naming the remedy, and `REMCTL_ICLOUD_SOURCE_TITLES="My Account Name"` (comma-separated)
overrides it.

## For Agents

Use JSON when scripting:

```bash
remctl today --json
remctl show Work --json
remctl search "query" --completed --json
remctl info 23880 --json
remctl doctor --for-agent --json
```

Use the absolute installed path in agent sessions because their `PATH` may not include `~/bin`. Normal `auto` mode routes every permission-bearing command through the signed host. `REMCTL_CAPABILITY_HOST=force` requires hosted execution and fails if the host is unavailable. `REMCTL_CAPABILITY_HOST=direct` deliberately bypasses the host for diagnostics or degraded recovery; in that mode the caller needs its own access. A custom `REMCTL_STORE_DIR` also forces direct execution and conflicts with `force`. Caller-side `REMCTL_BRIDGE_PATH` and `REMCTL_PRIVATE_PATH` overrides apply only to direct execution; the host always uses its sealed helpers.

`search` matches reminder titles and notes. By default it searches active reminders; pass `--completed` to include completed reminders too.

Do not use `--via-eventkit` by default. It never changes the execution route and RemCTL never chooses it automatically: `auto` uses the signed host, while `direct` or a custom store uses the caller. Use it only when a supported basic read explicitly needs degraded EventKit fidelity. JSON returns a wrapper with `source: "eventkit"`, `fidelity: "limited"`, and `items`; item identifiers are `eventKitId`, not RemCTL numeric `id`. Never pass `eventKitId` to `info`, `edit`, `done`, `delete`, `link`, `open`, `subtasks`, or any other numeric-ID command. If the task needs sections, tags, rich links, urgent state, templates, smart-list internals, or chainable IDs, grant Full Disk Access to the exact signed host instead.

For fast agent writes, call `remctl add ... --json`, use the returned `numericId` when present, then verify with `remctl info <numericId> --json`. `add --private` validates section/assignee/URL inputs before creating the reminder; if a private step still fails after creation, output is `{"status": "partial", "id", "numericId", "failed", "error"}` in JSON (text mode: `Created reminder #N but failed to apply <action>; re-run edit to finish. Do NOT re-run add (would duplicate).`). On `partial`, re-run `edit` to finish the metadata; never re-run `add`. For list moves, use the `id` returned by `remctl edit ... -l ... --json`; a verified clone-delete fallback can replace the original reminder and return `oldId` plus a new `id`. `info` includes private rich-link URLs, parent and subtask image attachments, EventKit alarms, location alarms, Early Reminders, and recurrence metadata, so agents should not need raw SQLite checks for ordinary reminder metadata verification. Parent reminder attachments also appear in list-command JSON (`show`, `today`, `upcoming`, `overdue`, `flagged`, `urgent`, `search`); each `attachments` entry includes a sha512-verified `path` as resolved by the signed host, or `path: null` with `resolved: false` when the attachment is a legacy row that was never downloaded on this Mac. The path does not grant a blocked caller access to the protected file.

Use JSON for automation when exact raw text matters. Human output is terminal-safe and strips control characters; JSON preserves the underlying Reminders values.

For smart-list automation, use `smart-list-create`, `smart-list-edit`, and `smart-list-delete` with `--private`, prefer `--smart-list-id` when editing or deleting an existing custom smart list, and verify with `remctl smart-lists --json`. `smart-lists --json` also reports smart-list pin state; on macOS 26, a successful smart-list pin can be verified from `pinnedDate` because the regular-list boolean can stay empty. Custom smart-list pinning uses the custom-list fetch on both Tahoe and Golden Gate. Verify a pin and its reversal from `smart-lists --json`, checking the same `objectUUID` and filter plus `pinned: true` with a positive `pinnedDate`, then `pinned: false` with no positive pin date. Built-in smart-list pinning is capability-gated: hosts that expose the generic fetch keep the existing behavior, while tested Tahoe 26.2 and Golden Gate 27.0 builds fail with a precise unsupported message before creating a save request. Reminders.app currently materializes only one included-list filter at a time; RemCTL rejects repeated included lists and list exclusions before writing. The smart-list command surface and examples are documented in [docs/commands.md#smart-lists](docs/commands.md#smart-lists); the private ReminderKit behavior and filter storage details are in [docs/private-metadata.md#smart-list-examples](docs/private-metadata.md#smart-list-examples).

For template automation, use `templates --json` and `template-info` to inspect saved templates. Use `template-create`, `template-apply`, and `template-delete` with `--private`; verify template rows with `templates --json` or `template-info`, and verify applied lists with `show <list> --json`. Template writes are list-level only: do not assume support for appending individual reminders to a template or excluding subtasks/due dates. Existing iCloud template links are read-only metadata.

For Groceries automation, use `lists --json` to detect `listType: "groceries"` and `grocery.locale`, then use `add --private --grocery` or `edit --private --grocery` only against an existing Groceries list. Verify with `show <list> --json` and check the reminder's `section` after categorization.

For shared-list assignments, call `remctl sharees LIST --json` first. `--assign` accepts a unique name, email/phone address, numeric sharee ID, object UUID, or `me`; prefer `address`, `id`, or `objectUUID` in automation because names can collide. Assignment writes require `--private` and a known target shared list, then verify with `remctl info ID --json` and inspect `assignment.assignee`.

Agents should pass deterministic due dates, using `YYYY-MM-DD` for all-day reminders and `YYYY-MM-DD HH:MM` for timed reminders after resolving the user's request in their timezone. If a due date is invalid, RemCTL exits before writing and emits a structured `invalid_due_date` JSON error on stderr with examples. Retry with a corrected date; do not create a reminder first and patch the due date afterward.

`flag`, `unflag`, and `add --flag` write the real flagged state through Reminders' AppleScript interface, because EventKit has no flagged API; there is no bridge fallback. In normal installed execution AppleScript runs inside the signed host, so Automation access belongs to that host. If the AppleScript write fails, `flag`/`unflag` exit 1 and emit `{"status": "error", "code": "applescript_flag_failed", "id", "message"}` on stderr instead of reporting a fake success. `add --flag` still creates the reminder and reports the failure as a `warnings` array (`["flag_not_set: …"]`) in the JSON payload; finish with `remctl edit <id> --private --flagged` rather than re-running `add`.

List names are resolved conservatively: exact match first, then case-insensitive match, then a normalized fallback that can handle decorative prefixes such as emoji. If more than one list matches, RemCTL fails before writing and asks for `--list-id`. Commands that target lists use the same rule: pass a name, or use `--list-id` for exact agent-safe targeting on `show`, `add`, `edit`, `link`, `export`, `section-create`, `section-rename`, `section-delete`, `list-edit`, `list-pin`, `list-unpin`, `list-rename`, `list-delete`, group membership/order edits, and smart-list list filters. Group commands target groups by name or `--group-id`; write commands that need a real list reject groups and name the child lists you can target. `list-create --private --group-id` is available when group names collide. `list-pin` and `list-unpin` can also target smart lists by name or `--smart-list-id`.

Do not mutate the Reminders SQLite database. Use RemCTL commands or EventKit.

For troubleshooting, trust `access.effective` in `doctor --for-agent --json`. A blocked `access.direct` result for Hermes or another caller is expected when the signed host is fully ready. If effective access fails, rerun `install.sh` or `remctl onboard` as directed and grant permissions only to the exact signed host. Do not add the caller app or interpreter.

After a repo update, rebuild and reinstall the complete signed host generation before testing:

```bash
git pull
./install.sh
hash -r
remctl --version
remctl doctor --for-agent --json
```

`./install.sh` requires `swiftc`, `clang`, a protected Python 3.13+, and a stable signing identity. When an existing signed host is present, it preserves that identity and its macOS privacy grants. It also accepts a valid explicit `REMCTL_CODESIGN_IDENTITY` or auto-detects an Apple Development identity. It builds and strictly verifies the signed app before transactionally replacing and restarting the service. Do not rerun onboarding after an existing signed-host upgrade unless `doctor` reports permission trouble. The official 1.7.1-to-1.8.0 transition is different: 1.7.1 had no host, so run onboarding and grant the new host access after installation. `remctl doctor` reports broker protocol v2, host permissions, effective routing, and sealed-helper readiness. RemCTL also checks the `remctl-private` protocol on first `--private` use; an outdated direct helper refuses to run with `remctl-private is outdated (protocol N < required M); re-run install.sh to rebuild.` Maintainers can send `{"action":"capabilities"}` to `remctl-private` for a read-only selector report; it creates only unsaved change objects and returns `saveCalled: false`.

## Docs

- [Installation and onboarding](docs/installation.md)
- [Command guide](docs/commands.md)
- [Smart-list command syntax](docs/commands.md#smart-lists)
- [Template command syntax](docs/commands.md#templates)
- [Private metadata writes](docs/private-metadata.md)
- [Smart-list private API notes](docs/private-metadata.md#smart-list-examples)
- [Template private API notes](docs/private-metadata.md#template-examples)
- [Architecture](docs/architecture.md)

## Project Layout

| Path | Purpose |
| --- | --- |
| `remctl` | Main Python CLI |
| `remctl-bridge.swift` | Swift/EventKit write helper source |
| `remctl-private.m` | Unsupported private ReminderKit metadata helper source |
| `remctl-permissions.swift` | Swift/AppKit guided Full Disk Access helper for the signed host target |
| `remctl-capability-host.swift` | Persistent signed AppKit host and native permission gate |
| `remctl-capability-host-Info.plist` | Capability Host app identity and metadata |
| `remctl-capability-host-launchagent.plist` | Per-user persistent host service template |
| `remctl_broker.py` | Protocol-v2 client/server transport and host runtime |
| `remctl_capability_policy.py` | Complete local/hosted command partition and broker policy |
| `remctl_capabilities.py` | Descriptor-backed input-file and TTY capabilities |
| `remctl_runtime.py` | Shared routing, paths, config, date windows, and safety helpers |
| `remctl_images.py` | Attachment file resolution and inline terminal image rendering |
| `remctl_serialization.py` | Shared reminder JSON serialization |
| `remctl_smart_lists.py` | Smart-list filter decoding and safe v1 encoding |
| `scripts/build_capability_archive.py` | Builds the sealed, sourceless Python runtime archive |
| `scripts/live_edit_matrix.py` | Opt-in live edit-mode matrix for due/display/alarm regressions |
| `scripts/live_private_matrix.py` | Opt-in live private command matrix using disposable Reminders data |
| `install.sh` | Transactional signed-host installer and bootstrap script |
| `uninstall.sh` | Stops and removes the exact host service, app, socket, CLI files, and optional config |

## License

MIT. See [LICENSE](LICENSE).
