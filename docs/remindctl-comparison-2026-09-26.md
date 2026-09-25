# RemCTL and remindctl: gaps closed on 2026-09-26

Hermes Agent's bundled `apple-reminders` skill runs [`remindctl`](https://github.com/steipete/remindctl), a separate Reminders CLI. An audit on 2026-09-25 compared it with RemCTL 2.0 at `6666a25` and found three feature gaps plus a set of MCP usability gaps. This page records what changed. Hermes PR 51466 adds Notes, Mail, Numbers, and Photos; it does not add a Reminders integration, so `remindctl` is the right comparison.

Sources:

- remindctl 0.3.8 at `cdd7a06`: [README](https://github.com/steipete/remindctl/blob/cdd7a06b85272980067f0e5b386fea9e10be0cf9/README.md), [search matching](https://github.com/steipete/remindctl/blob/cdd7a06b85272980067f0e5b386fea9e10be0cf9/Sources/remindctl/CommandHelpers.swift), [scoped search](https://github.com/steipete/remindctl/blob/cdd7a06b85272980067f0e5b386fea9e10be0cf9/Sources/remindctl/Commands/SearchCommand.swift), [completion](https://github.com/steipete/remindctl/blob/cdd7a06b85272980067f0e5b386fea9e10be0cf9/Sources/remindctl/Commands/CompleteCommand.swift), [location](https://github.com/steipete/remindctl/blob/cdd7a06b85272980067f0e5b386fea9e10be0cf9/Sources/RemindCore/EventKitLocation.swift).
- Hermes skill at `34343e7`: [apple-reminders/SKILL.md](https://github.com/NousResearch/hermes-agent/blob/34343e79ab603f2a6f1c1b7597a44c00c6e2ce6f/skills/apple/apple-reminders/SKILL.md).
- RemCTL before: `6666a25`. After: the commit that adds this page. Function names below are in `remctl`, `remctl_mcp.py`, and `remctl-bridge.swift`.

These findings come from reading code and documentation. `remindctl` was not run against real reminders.

## Feature gaps

| Capability | remindctl 0.3.8 | RemCTL before | RemCTL after |
| --- | --- | --- | --- |
| Search fields | Title, notes, and URL, ignoring case and accents (`reminder(_:matchesSearch:)`) | Title and notes with SQL `LIKE`, which ignores case only for ASCII (`q_search`) | Title, notes, and saved rich links, ignoring case and accents; `%`, `_`, and `\` stay literal (`search_fold`, `_search_where`) |
| Search scope | `--list` or `--list-id` | None | `--list` (exact, then case-insensitive, then normalized; duplicate names stop with ids) or `--list-id` |
| Result limit | None; prints every match | 100 rows, cut off silently (`LIMIT 100`) | Pages of 1 to 500 with `total`, `hasMore`, and `nextOffset` (`--limit`, `--offset`); unpaged JSON warns on stderr |
| Batch completion | `complete 1 2 3`, by listing index or id prefix | One id | Up to 50 numeric ids: all looked up first, repeats written once, per-id results (`run_reminder_state_batch`) |
| Batch deletion | `delete 4A83 --force`, several ids | One id | Up to 50 ids, one confirmation listing each reminder, `--force` still required with `--json` |
| Repeating reminder after a failed write | One EventKit save | A bridge timeout fell back to AppleScript, which could complete the reminder a second time and skip an occurrence | Retried only when the bridge reported it saved nothing; otherwise `completion_uncertain`. A batch stops after an uncertain or stalled write and within a 60-second budget (`write_reminder_state`, `bridge_outcome_uncertain`) |
| Location from an address | Geocodes and uses the first match (`placemarks.first`), 30-second timeout; its README geocodes `"Home"` | Coordinates only | `--location-address`: geocoded in the signed host with a 10-second limit and cancellation; used only when it is the only match, street-sized, names the street typed, and the query gives a town or postal code; otherwise, or for a personal label such as `Home`, nothing is created (`lookup_location`, `address_mismatch_reason`, `runGeocode`) |
| Reviewing a location first | Not available | Not available | `location-lookup` and the `resolve_location` tool, read-only |
| Radius checks | Finite and positive | Positive only; `nan` passed | Finite, 1 to 100000 meters, before any lookup or write |

## MCP gaps

`remindctl` has no MCP server; Hermes reaches it through shell commands described in a skill. RemCTL already exposed everything below through its generic `run` tool. The gap was that an agent had to know the command syntax.

| Need | Before | After |
| --- | --- | --- |
| Scoped, paged search | `search` took `query` and `include_completed` | Adds `list`, `list_id`, `limit`, `offset`; always returns a page |
| Batches | One `reminder_id` | `reminder_ids` (up to 50) on `set_completion` and `delete_reminder`; per-id results are kept even though the call is an error unless every id succeeded |
| Synced tags and rich links | `tags` and `url` fell back to `#hashtags` and notes; native versions needed `run` | Same fallbacks by default; `private: true` makes them synced tags and rich links |
| Sections, subtasks, assignment, Early Reminders, urgent, location alarms | `run` only | Typed fields on `create_reminder` and `update_reminder`, refused unless `private: true` |
| Metadata those writes need | `run` with `sections` (names only, keyed by list name, no section ids) or `sharees` | `get_list`: section ids and sharees for one list |
| Lists | `run` only | `create_list` (returns the numeric id) and `update_list` |
| Address check | None | `resolve_location`, the only tool marked `openWorldHint` |
| Ids after a move | `update_reminder` already returned `id` and `oldId` after a clone-delete move | Unchanged; now stated in the tool description |
| Partial create | Already kept `status: partial` with the id | Unchanged; now stated in the tool description |

Specialist features stay on `run`: smart lists, templates, groups, ordering, images, Groceries sorting, import, and export.

## What RemCTL already had

These were not gaps, and `remindctl` does not offer them, by its own README: sections, synced tags, smart lists, templates, list groups, images, urgent state, Early Reminders, shared-list assignment, manual ordering, reads from Reminders' database (display dates, subtasks, rich links), a signed host that keeps permissions off the terminal, an MCP server with a widget, and access from other devices over Tailscale. RemCTL also keeps stable numeric ids; `remindctl`'s listing indexes change when the list changes.

## Verification

- `tests/test_search_batch_location.py`: rich-link-only matches in the right list; case, accent, and literal matching; 150 matches over two pages with honest counts; duplicate list names needing `--list-id`; batches with repeated, missing, and repeating ids; an uncertain repeating completion that is not repeated; batch delete confirmation; address resolution with a mocked geocoder, and ambiguous, area-wide, missing, timed-out, denied, personal-label, bad-radius, and conflicting input creating nothing.
- `tests/test_list_info.py`: duplicate section names keep distinct ids; the new list id ignores an older list with the same name.
- `tests/test_mcp_typed_tools.py`: the real tool schemas, the private opt-in, every new argument parsed by the real CLI parser, readback of tags, assignment, subtasks, due and display dates, and alarms, batch results surviving a nonzero exit, and the real stdio server with a stand-in CLI.
- The official MCP Python SDK 2.2 client listed all 19 tools and validated the new tools' structured results against their output schemas.
- The new `remctl-bridge` geocoded real addresses read-only: street addresses returned one match with a region of about 70 meters; `Rome` and `Springfield` each returned one match covering 38 and 15 kilometers, which RemCTL reports as imprecise; `Home` and nonsense returned no match. `Main Street 1` returned `1 Rykneld Court` in England and `Via Roma 1` a Via Roma in Grottaferrata, each as a single street-sized match; RemCTL reports both as unconfirmed.
- An independent review of the patch found that first rule gap, a batch that could outlast the MCP timeout, and a too-trusting bridge outcome check. All three are fixed and covered by tests.
