# Multi-account support (optional extension)

RemCTL's core is single-account by design: it reads the one "live" Reminders
store and writes through iCloud. Reminders keeps a **separate SQLite store per
connected account**, so Exchange, other CalDAV, and Local accounts are
invisible to the core tool.

`remctl_accounts.py` adds support for those accounts as an **optional
module**. Core imports it inside `try/except ImportError`, and nothing it adds
is active until the user opts in. At runtime, removing the file returns RemCTL
to stock. The installer and the sealed host archive use a fixed file list, so
removing it from a checkout before `install.sh` also means dropping it from
`install.sh`, `uninstall.sh`, and `scripts/build_capability_archive.py`.

## Integration contract

The footprint in core `remctl` is **23 added lines, 0 modified or deleted
lines**, in four hunks:

| Hook | Purpose |
|------|---------|
| `try: import remctl_accounts` | Optional; `ImportError` leaves `remctl_accounts = None` |
| `_db_opener = None` in `open_db()` | Extension point letting a caller redirect reads to another store |
| `remctl_accounts.register_cli(p, sub)` | Adds `--account`/`--all-accounts` and the `accounts`/`config` commands |
| `remctl_accounts.install(cmds, a, sub)` | Wraps the command dispatch table |

Every hook is guarded by `if remctl_accounts:`. With the module absent, core
runs exactly as upstream; upstream's runtime suite passes without it.

### Capability Host integration

The signed Capability Host keeps closed lists of commands and options, so the
extension also needs three small hooks outside `remctl`:

| File | Change | Why |
|------|--------|-----|
| `remctl_runtime.py` | `register_extension_commands(local=, hosted=)` (+27 lines) | The host refuses any parser command that is not classified. `accounts` is hosted (it reads the stores); `config` is local (it only edits `config.json`, like `setup`). Core commands can never be reclassified. |
| `remctl_capability_policy.py` | Reads the command sets at call time (6 lines) | It previously copied them at import, before an extension could register. |
| `remctl_mcp.py` | Optional import of the extension's `RUN_GLOBAL_OPTIONS` and `RUN_FORBIDDEN_COMMANDS` (+9 lines) | The MCP `run` guard must know `--account` takes a value; otherwise `--account X setup` reads `X` as the command and slips past the forbidden-command check. `config` is forbidden over MCP like `setup`. |

`install.sh`, `uninstall.sh`, and `scripts/build_capability_archive.py` list
`remctl_accounts.py` alongside the other `remctl_*.py` modules, so it ships in
the sealed host archive (the archive builder rejects unlisted modules).

`register_cli()` also wraps three client-side routing helpers in `remctl`:

- **Custom stores run only in direct mode.** `REMCTL_DB`, config `dbPath`, and
  config `storeDir` route the command directly, exactly like core's
  `REMCTL_STORE_DIR`; `REMCTL_CAPABILITY_HOST=force` with any of them is an
  error. Inside the host they are never honored, so the host only ever reads
  its pinned Reminders store.
- **Env scope reaches the host.** The host runs commands in a sanitized
  environment, so `REMCTL_ACCOUNT_SCOPE` is forwarded as `--account NAME` or
  `--all-accounts` when the command supports it. Explicit flags win.
  `accountScope` in `config.json` is read by the host directly.
- **No-command splash.** `remctl --account Work` keeps its scope when the host
  runs the implied `today`.

## Why wrapping instead of editing commands

No core command is modified. Instead:

- **Reads** — `remctl._db_opener` is bound to the target account's store, and
  the *unmodified* core handler runs against it.
- **Writes** — an `account` field is injected into outgoing bridge payloads;
  `remctl-bridge` resolves a `(list, account)` pair to a stable calendar.
- **Aggregation** — for `lists`, `search`, `today`, `show`, etc. the core
  handler is run once per account and its output merged (JSON payloads are
  parsed and tagged with `account`/`accountType`; human output gets
  per-account headers). Both streams are captured per account, so an account
  that simply lacks the requested list contributes nothing instead of leaking
  a "not found" error; if every account fails, the error is surfaced and the
  exit code preserved.

The split is **read vs. act**, not reminder vs. list. Reads aggregate, because
showing every match is more useful than refusing. Commands that mutate a single
item (`add`, `done`, `edit`, `delete`, `list-delete`, `section-create`, ...)
refuse an ambiguous target, because acting on the wrong account's copy is not
recoverable. `AGGREGATE_COMMANDS`, `REMINDER_TARGET_COMMANDS` and
`LIST_TARGET_COMMANDS` encode that split, and a test asserts every name in them
is a real subcommand — a typo there silently disables the flags for a command.

The practical consequence: **upstream changes to those commands are inherited
automatically** rather than needing to be re-merged. When upstream added list
groups and inline images, multi-account output gained both for free.

## Opting in

Nothing above activates unless the user asks for it:

```bash
remctl lists --all-accounts          # every connected account
remctl lists --account Exchange      # one account
remctl --account Exchange lists      # flag works before the command too
export REMCTL_ACCOUNT_SCOPE=all      # session default
remctl config accountScope all       # persistent default
```

With no opt-in, `install()` returns the dispatch table untouched.

New commands:

```bash
remctl accounts                      # list connected accounts and types
remctl config [KEY] [VALUE]          # accountScope | storeDir | dbPath
```

## Account types

Account type comes from the store itself: each store's `REMCDAccount` record
has a type code (1 Local, 2 iCloud, 3 CalDAV, 4 Exchange, 5 LocalInternal).
This is right even before an account's first sync, when it has no lists yet.
For an unknown code the extension falls back to a heuristic over
`ZREMCDREPLICAMANAGER` identifiers. EventKit, via the bridge, can still refine
the label when it names a concrete kind (Exchange, …); it reports iCloud and
every other CalDAV account as plain `CalDAV`, so that label never overrides the
store's.

Google accounts are not a Reminders source: Google's CalDAV server syncs calendar
events but not tasks, so macOS offers no Reminders option for them and no Google
store ever appears.

Account *discovery* itself is type-agnostic: it enumerates every
`Data-*.sqlite` store and reads the `REMCDAccount` entity, so any account type
Reminders supports is found. It skips `LocalInternal` (an internal bookkeeping
store), accounts marked for deletion, and stores with no account record at all.
Reminders leaves those empty stores behind when an account is re-added or fails
its first sync, and listing them would show phantom "Local" accounts.

## Reminders without a CloudKit identifier

Only iCloud reminders carry `ZCKIDENTIFIER`. Core refuses to modify a reminder
without one rather than risk a title-based fallback — which would make every
Exchange/CalDAV reminder read-only. Those stores keep each reminder's EventKit
`calendarItemIdentifier` in `ZDACALENDARITEMUNIQUEIDENTIFIER` instead (verified
on Exchange and CalDAV), so when an account is explicitly targeted the
extension hands that **exact** identifier to core's normal write path.

Only if a row has no stored identifier does the extension ask EventKit by
`(calendar, title)` (the bridge's `find_reminder`), and only when that title is
unique in its list. Otherwise the row is left alone and core's refusal stands,
so a same-titled sibling is never modified by mistake.

`add` uses the same column in reverse: core looks the new row up by
`ZCKIDENTIFIER` to report `numericId`, so inside an account context the
extension also matches the bridge's returned identifier against
`ZDACALENDARITEMUNIQUEIDENTIFIER` (exact match only).

**Flags.** Exchange and CalDAV reminders have no flag attribute: Reminders
accepts the AppleScript flag write and silently drops it. The extension
refuses `flag`, `unflag`, and `add --flag` on those accounts before writing
(`code: "flag_unsupported_for_account"`), instead of letting core report a
flag that never stuck.

## Bridge changes

`remctl-bridge.swift` gains a `list_calendars` action, a `find_reminder`
action, and `calendarIdentifier` / `account` fields on `Command`.

Upstream's `findList()` is **byte-identical** — its body is untouched, so the
default single-account path and its iCloud-only write restriction are provably
unchanged. Multi-account resolution lives in a separate `findListScoped()`,
which delegates straight back to `findList()` whenever no account or
`calendarIdentifier` is supplied:

```swift
let wantsScope = !(calendarIdentifier ?? "").isEmpty || !(account ?? "").isEmpty
guard wantsScope else {
    return findList(store, name: name, listId: listId)
}
```

Call sites prepend a scoped branch rather than replacing upstream's, so
upstream's own `if let list = cmd.list` / `else if let listId` arms remain in
place. The 8 removed lines are: 2 `if` keywords becoming `} else if`, 2
`rename_list`/`delete_list` one-liners gaining a scoped ternary, and 4 lines in
`create_list` where the iCloud source lookup is wrapped in an `else` (preserved
verbatim inside it).

The bridge must be recompiled via `install.sh`.

## Changes to upstream's test suite

Two upstream assertions change, each by one line:

```diff
# tests/test_cli.py
-        self.assertIn("  add,", output)
+        self.assertIn("add,", output)
```

That assertion probes the wrapped "Available commands:" columns by checking the
first line begins with two spaces then `add,`. The command list is alphabetical,
and `accounts` sorts before `add`, so the line becomes `"  accounts, add, …"`.
`add` is still listed and the error message is still correct — only the leading
whitespace moved.

```diff
# tests/test_runtime.py
-        self.assertEqual(remctl_runtime.LOCAL_COMMANDS, expected_local)
+        self.assertEqual(
+            remctl_runtime.LOCAL_COMMANDS - remctl_runtime.EXTENSION_COMMANDS, expected_local
+        )
```

The exact set of core local commands is still asserted; `config` is excluded
because the extension registers it when the suite has loaded `remctl`.

Nothing else in upstream's tests is touched. Upstream's own partition test
(every parser command classified exactly once) and the MCP run-guard test
(every top-level option known) pass unchanged with the extension loaded.

Determinism note: `tests/conftest.py` pins pytest runs to an empty config
directory and clears the `REMCTL_*` environment overrides. Without it a
contributor with a stored `accountScope` sees extra failures that do not
reproduce elsewhere — those tests assert `main()` dispatches to exactly
`cmd_show`/`cmd_done`, and a stored scope opts the run into the wrapped
handlers. `tests/test_accounts.py` isolates its own environment, so it is
deterministic under `unittest` too.

## Tests

`tests/test_accounts.py` covers the extension in isolation (107 tests):
discovery and ranking, config precedence, scope resolution, the account
context manager and its restoration, JSON merging, target disambiguation,
dispatch installation, bridge payload handling, identifier backfill, and the
Capability Host integration (command classification, the MCP run guard,
custom-store routing, and scope forwarding), account-type detection against
real store schemas, exact identifier targeting for non-iCloud reminders
(including same-titled siblings), the numericId lookup, and flag refusal.
