"""Optional multi-account extension for RemCTL.

RemCTL's core is single-account by design: it reads the one "live" Reminders
store and writes through iCloud. Reminders, however, keeps a separate SQLite
store per connected account (iCloud, Exchange, Google/CalDAV, Local), so any
non-iCloud account is invisible to the core tool.

This module adds that support **without modifying core behavior**. It attaches
through four small hooks in `remctl`:

    remctl_accounts.register_cli(p, sub)    # adds flags + `accounts`/`config`
    remctl_accounts.install(cmds, a, sub)   # wraps the command dispatch table
    remctl._db_opener                       # redirects core's open_db()
    import remctl_accounts                  # optional; absent == stock RemCTL

Nothing here is reachable unless the user opts in with `--account NAME`,
`--all-accounts`, `REMCTL_ACCOUNT_SCOPE`, or a stored `accountScope` config
value. With no opt-in, `install()` returns the dispatch table untouched and
every core command runs exactly as it does upstream.

Design note — why wrapping instead of per-command edits:

Core commands are reused as-is. To read another account we bind
`core._db_opener` to that account's store and run the *unmodified* core
handler; to write to another account we inject an `account` field into
outgoing bridge payloads (remctl-bridge resolves a (list, account) pair to a
stable calendar). Because no core command is rewritten, upstream changes to
those commands are inherited automatically rather than needing to be
re-merged here.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
from collections import namedtuple
from pathlib import Path

class _Core:
    """Live attribute view over the running remctl script's namespace.

    The core script has no .py suffix, so it cannot be imported by name. It
    hands us its `globals()` instead. Lookups resolve at call time (core is
    still mid-import when bind() runs), and assignments write back into core
    -- which is how `_db_opener` and the bridge wrappers are installed.
    """

    def __init__(self, namespace):
        object.__setattr__(self, "_ns", namespace)

    def __getattr__(self, name):
        try:
            return object.__getattribute__(self, "_ns")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        object.__getattribute__(self, "_ns")[name] = value


core = None


def bind(namespace):
    """Receive the running remctl module's namespace."""
    global core
    core = _Core(namespace)


Account = namedtuple("Account", ["store_path", "name", "type"])

ACCOUNT_SCOPE_ENV = "REMCTL_ACCOUNT_SCOPE"
STORE_DIR_ENV = "REMCTL_STORE_DIR"
DB_ENV = "REMCTL_DB"

# Reminders keeps an internal bookkeeping account that holds no user data.
HIDDEN_ACCOUNT_NAMES = {"LocalInternal"}

def config_file():
    return core.CONFIG_DIR / "config.json"

# Commands that aggregate across accounts: run once per account, merge output.
# These are pure reads, so showing every match is more useful than refusing --
# `show`/`sharees` name a list, and a name that exists in two accounts simply
# yields two blocks rather than an ambiguity error.
AGGREGATE_COMMANDS = {
    "lists", "groups", "search", "today", "flagged", "urgent",
    "upcoming", "overdue", "tags", "smart-lists", "templates", "stats",
    "show", "sections", "sharees",
}

# Commands that act on a single reminder identified by numeric id.
REMINDER_TARGET_COMMANDS = {
    "info", "done", "undone", "edit", "delete", "flag", "unflag",
    "subtasks", "open", "link",
}

# Commands that act on a single list.
# Commands that act on a single list. These keep refusing an ambiguous name:
# creating or deleting in the wrong account is not recoverable.
LIST_TARGET_COMMANDS = {
    "add", "export", "import", "list-edit", "list-delete",
    "section-create", "section-rename", "section-delete",
}

# Commands whose output is inherently one account's: offering --all-accounts
# would imply a merge they cannot perform (IDs collide across accounts).
SINGLE_ACCOUNT_COMMANDS = {"export", "import"}

# Bridge actions that write; these get an `account` hint when scoped.
BRIDGE_WRITE_ACTIONS = {
    "create", "update", "delete", "complete", "uncomplete", "flag", "unflag",
    "create_list", "rename_list", "delete_list",
}

_ACCOUNT_CACHE = None


# ── Configuration ────────────────────────────────────────────────────────────

def load_config():
    """Return the stored config dict, or {} when missing/invalid."""
    try:
        data = json.loads(config_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data):
    config_file().parent.mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps(data, indent=2) + "\n")


def store_dir():
    """Reminders Stores directory, honoring env then config then core default."""
    override = os.environ.get(STORE_DIR_ENV, "").strip() or load_config().get("storeDir", "")
    return Path(override).expanduser() if override else core.STORE_DIR


def db_override():
    """A pinned store file (REMCTL_DB / config dbPath), or None."""
    raw = os.environ.get(DB_ENV, "").strip() or load_config().get("dbPath", "")
    return Path(raw).expanduser() if raw else None


# ── Account discovery ────────────────────────────────────────────────────────

def _bridge_payload(request):
    """Raw bridge payload for *request*, or None.

    core.bridge_call() unwraps only dict payloads, so list-returning actions
    such as list_calendars come back as None through it. Go through
    bridge_call_result() and read the payload directly.
    """
    try:
        if not core.bridge_available():
            return None
        result = core.bridge_call_result(request)
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("returncode") != 0:
        return None
    payload = result.get("payload")
    if isinstance(payload, dict) and payload.get("status") != "error":
        return payload
    # bridge_call_result() replaces any non-dict JSON (list_calendars returns a
    # list) with a synthesized error dict, so re-read the raw stdout for those.
    try:
        return json.loads(result.get("stdout") or "")
    except ValueError:
        return None


def _bridge_source_types():
    """{source title: EventKit source type} from the bridge, or {} if absent.

    EventKit is authoritative about account types, so this correctly labels
    Google/CalDAV and any other account type Reminders supports. The DB
    heuristic below is only a fallback for when the bridge is unavailable.
    """
    result = _bridge_payload({"action": "list_calendars"})
    if not isinstance(result, list):
        return {}
    types = {}
    for entry in result:
        if isinstance(entry, dict) and entry.get("sourceTitle"):
            types.setdefault(entry["sourceTitle"], entry.get("sourceType"))
    return types


def _account_type_from_identifiers(identifiers):
    """Best-effort account type from Reminders replica identifiers."""
    joined = " ".join(identifiers).lower()
    if "exchangesync" in joined:
        return "Exchange"
    if "com.apple.reminders" in joined or "cloudkit" in joined:
        return "iCloud"
    if "google" in joined:
        return "Google"
    if "caldav" in joined or "dav" in joined:
        return "CalDAV"
    return "Local"


# EventKit reports iCloud as plain "CalDAV"; these labels carry no more
# information than the store heuristic already derived.
GENERIC_SOURCE_TYPES = {"", "CalDAV", "Local", None}


def _merge_account_type(db_type, eventkit_type):
    """Prefer whichever label is more specific.

    EventKit wins when it names a concrete account kind (Exchange, Birthdays,
    ...). When it only says "CalDAV" -- which is what iCloud and Google both
    report -- the store heuristic is the more useful answer.
    """
    if eventkit_type and eventkit_type not in GENERIC_SOURCE_TYPES:
        return eventkit_type
    if db_type and db_type != "Local":
        return db_type
    return eventkit_type or db_type


def _store_account_info(path):
    """Return (name, type) for the account stored at *path*, or None."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        # Derive the account entity number dynamically — never hardcode Z_ENT.
        ent_row = conn.execute(
            "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME='REMCDAccount'"
        ).fetchone()
        if ent_row is None:
            return None
        name_row = conn.execute(
            "SELECT ZNAME FROM ZREMCDOBJECT WHERE Z_ENT=? AND ZNAME IS NOT NULL LIMIT 1",
            (ent_row[0],),
        ).fetchone()
        # A freshly added account may not have synced its name yet.
        name = name_row[0] if name_row else None

        try:
            identifiers = [
                r[0] for r in conn.execute(
                    "SELECT ZIDENTIFIER FROM ZREMCDREPLICAMANAGER WHERE ZIDENTIFIER IS NOT NULL"
                ).fetchall()
            ]
        except sqlite3.Error:
            identifiers = []

        acct_type = _account_type_from_identifiers(identifiers)
        if not name:
            if identifiers:
                name = f"{acct_type} ({identifiers[0].split('/')[0][:8]})"
            else:
                name = f"{acct_type} ({Path(path).stem[-8:]})"
        return (name, acct_type)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def discover_accounts(*, force_refresh=False):
    """Every real Reminders account, ranked by the core store scorer.

    accounts[0] is always the store core's find_main_db_path() would pick, so
    the default single-account path stays identical with this module loaded.
    """
    global _ACCOUNT_CACHE
    if _ACCOUNT_CACHE is not None and not force_refresh:
        return _ACCOUNT_CACHE

    if core.reminders_store_access_error():
        _ACCOUNT_CACHE = []
        return _ACCOUNT_CACHE

    pinned = db_override()
    if pinned:
        if not pinned.exists():
            _ACCOUNT_CACHE = []
            return _ACCOUNT_CACHE
        info = _store_account_info(pinned)
        _ACCOUNT_CACHE = [Account(pinned, info[0] if info else pinned.stem,
                                  info[1] if info else "Local")]
        return _ACCOUNT_CACHE

    bridge_types = _bridge_source_types()
    scored = []
    for path in store_dir().glob("Data-*.sqlite"):
        info = _store_account_info(path)
        if info is None:
            continue
        name, acct_type = info
        if name in HIDDEN_ACCOUNT_NAMES:
            continue
        # Prefer EventKit's view of the account type when we have it.
        acct_type = _merge_account_type(acct_type, bridge_types.get(name))
        scored.append((core.reminders_db_score(path), Account(path, name, acct_type)))

    # Same ordering key as core find_main_db_path().
    scored.sort(key=lambda item: item[0], reverse=True)
    _ACCOUNT_CACHE = [acct for _, acct in scored]
    return _ACCOUNT_CACHE


# ── Scope resolution ─────────────────────────────────────────────────────────

def _requested_account_names(a):
    names = getattr(a, "account", None)
    if not names:
        return []
    return list(names) if isinstance(names, list) else [names]


def is_multi_account_mode(a):
    """True when the caller explicitly opted into account selection."""
    if getattr(a, "all_accounts", False):
        return True
    if _requested_account_names(a):
        return True
    if os.environ.get(ACCOUNT_SCOPE_ENV, "").strip():
        return True
    return bool(load_config().get("accountScope", ""))


def resolve_account_scope(a):
    """Accounts this invocation should act on, highest-precedence first.

    --account NAME > --all-accounts > REMCTL_ACCOUNT_SCOPE > config
    accountScope > the single default account.
    """
    everything = discover_accounts()
    if not everything:
        return []

    def by_name(names):
        lowered = [n.lower() for n in names]
        matched = [acct for acct in everything if acct.name.lower() in lowered]
        missing = [n for n in names
                   if not any(acct.name.lower() == n.lower() for acct in everything)]
        if missing:
            print(f"Error: unknown account(s): {', '.join(missing)}. "
                  f"Available: {', '.join(acct.name for acct in everything)}",
                  file=sys.stderr)
            sys.exit(1)
        return matched

    requested = _requested_account_names(a)
    if requested:
        return by_name(requested)
    if getattr(a, "all_accounts", False):
        return everything
    for source in (os.environ.get(ACCOUNT_SCOPE_ENV, "").strip(),
                   load_config().get("accountScope", "")):
        if source:
            return everything if source.lower() == "all" else by_name([source])
    return [everything[0]]


# ── Running core commands against a chosen account ───────────────────────────

def _calendar_id_for(list_name, account):
    """EKCalendar identifier for (list_name, account), or None."""
    calendars = _bridge_payload({"action": "list_calendars"})
    if not isinstance(calendars, list):
        return None
    for entry in calendars:
        if (isinstance(entry, dict)
                and entry.get("title") == list_name
                and (entry.get("sourceTitle") or "") == account.name):
            return entry.get("calendarIdentifier")
    return None


def _with_identifier(row, account):
    """Backfill a missing stable identifier from EventKit.

    Only iCloud reminders carry a CloudKit identifier (ZCKIDENTIFIER). Core
    refuses to modify a reminder without one rather than risk a title-based
    fallback, which would make every Exchange/Google reminder read-only. Here
    we ask EventKit for the real item identifier so core's normal bridge path
    works unchanged.

    Caveat: EventKit is queried by (calendar, title), so if a list holds two
    reminders with the identical title the first is returned. Core's own
    guard is stricter -- it simply refuses -- so this trades an exact refusal
    for a resolvable-but-ambiguous match only on accounts that would
    otherwise be unusable.
    """
    if row is None:
        return row
    if core._row_get(row, "ZCKIDENTIFIER"):
        return row
    title = core._row_get(row, "ZTITLE")
    list_name = core._row_get(row, "list_name")
    if not title or not list_name:
        return row
    calendar_id = _calendar_id_for(list_name, account)
    if not calendar_id:
        return row
    found = _bridge_payload({
        "action": "find_reminder",
        "calendarIdentifier": calendar_id,
        "title": title,
    })
    identifier = found.get("calendarItemIdentifier") if isinstance(found, dict) else None
    if not identifier:
        return row
    enriched = dict(row)
    enriched["ZCKIDENTIFIER"] = identifier
    return enriched


@contextlib.contextmanager
def account_context(account):
    """Point core's open_db() and bridge writes at *account* for the duration.

    This is what lets unmodified core commands operate on a non-default
    account: reads follow `_db_opener`, writes carry an `account` hint that
    remctl-bridge uses to resolve the correct calendar.
    """
    prev_opener = core._db_opener
    prev_call = core.bridge_call
    prev_call_result = core.bridge_call_result
    prev_q_reminder = core.q_reminder

    def opener():
        conn = sqlite3.connect(f"file:{account.store_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def tag(payload):
        if isinstance(payload, dict) and payload.get("action") in BRIDGE_WRITE_ACTIONS:
            if "account" not in payload:
                payload = {**payload, "account": account.name}
        return payload

    core._db_opener = opener
    core.bridge_call = lambda data, *args, **kw: prev_call(tag(data), *args, **kw)
    core.bridge_call_result = lambda data, *args, **kw: prev_call_result(tag(data), *args, **kw)
    core.q_reminder = lambda db, pk, *args, **kw: _with_identifier(
        prev_q_reminder(db, pk, *args, **kw), account)
    try:
        yield
    finally:
        core._db_opener = prev_opener
        core.bridge_call = prev_call
        core.bridge_call_result = prev_call_result
        core.q_reminder = prev_q_reminder


CaptureResult = namedtuple("CaptureResult", ["out", "err", "ok"])


def _run_capture(handler, a, account):
    """Run a core handler against *account* and capture its output.

    Both streams are captured. A non-zero exit for one account is not fatal
    during aggregation -- a list simply may not exist in every account -- so
    the failure is reported through `ok` and its stderr is held back rather
    than interleaved with the accounts that did produce output.
    """
    out, err = io.StringIO(), io.StringIO()
    ok = True
    try:
        with (account_context(account),
              contextlib.redirect_stdout(out),
              contextlib.redirect_stderr(err)):
            handler(a)
    except SystemExit as exc:
        ok = exc.code in (0, None)
    except core.RemindersDBUnavailable:
        ok = False
    return CaptureResult(out.getvalue(), err.getvalue(), ok)


# ── Aggregating across accounts ──────────────────────────────────────────────

def _tag_json_items(parsed, account):
    """Stamp account metadata onto a parsed JSON payload, recursively."""
    if isinstance(parsed, list):
        for item in parsed:
            _tag_json_items(item, account)
    elif isinstance(parsed, dict):
        parsed["account"] = account.name
        parsed["accountType"] = account.type
        for child in parsed.get("children") or []:
            _tag_json_items(child, account)
    return parsed


def _merge_json(results):
    """Merge per-account JSON payloads into one document."""
    lists, objects = [], {}
    for account, text in results:
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        _tag_json_items(parsed, account)
        if isinstance(parsed, list):
            lists.extend(parsed)
        else:
            objects[account.name] = parsed

    if objects and not lists:
        merged = {"accounts": objects}
        # Numeric-only payloads (stats) also get a combined total.
        totals = {}
        for payload in objects.values():
            for key, value in payload.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
        if totals:
            merged["total"] = totals
        return merged
    return lists


def _aggregate(handler, a, scope):
    """Run *handler* once per account and emit a combined result."""
    multi = len(scope) > 1
    captured = [(account, _run_capture(handler, a, account)) for account in scope]
    results = [(account, r.out) for account, r in captured if r.ok]

    if getattr(a, "json", False) or getattr(a, "format", None) == "json":
        print(json.dumps(_merge_json(results), indent=2, ensure_ascii=False))
        return

    printed = False
    for account, text in results:
        if not text.strip():
            continue
        if multi:
            if printed:
                print()
            print(core.C.bold(f"  {account.name}"))
        printed = True
        print(text.rstrip("\n"))
    if printed:
        return

    # Nothing anywhere. Surface a real error if every account reported one,
    # otherwise let the core command print its own empty-state message.
    errors = [r.err for _, r in captured if not r.ok and r.err.strip()]
    if errors and len(errors) == len(captured):
        sys.stderr.write(errors[0])
        sys.exit(1)
    for account in scope[:1]:
        with account_context(account):
            handler(a)


# ── Targeting a single account ───────────────────────────────────────────────

def _accounts_holding_reminder(scope, pk):
    found = []
    for account in scope:
        try:
            conn = sqlite3.connect(f"file:{account.store_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        try:
            if core.q_reminder(conn, pk):
                found.append(account)
        except Exception:
            pass
        finally:
            conn.close()
    return found


def _accounts_holding_list(scope, name, list_id):
    found = []
    for account in scope:
        try:
            conn = sqlite3.connect(f"file:{account.store_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        try:
            ref = core.resolve_list_ref(conn, name=name, list_id=list_id, allow_groups=True)
            if ref and not ref.get("error"):
                found.append(account)
        except Exception:
            pass
        finally:
            conn.close()
    return found


def _pick_target_account(a, scope, command):
    """The single account a targeted command should act on."""
    if len(scope) == 1:
        return scope[0]

    label, candidates = None, None
    pk = getattr(a, "id", None)
    if command in REMINDER_TARGET_COMMANDS and isinstance(pk, int):
        label, candidates = f"reminder id {pk}", _accounts_holding_reminder(scope, pk)
    elif command in LIST_TARGET_COMMANDS:
        name = getattr(a, "list", None) or getattr(a, "name", None)
        list_id = getattr(a, "list_id", None)
        if name or list_id is not None:
            label = f"list {name!r}" if name else f"list id {list_id}"
            candidates = _accounts_holding_list(scope, name, list_id)

    if candidates is None:
        return scope[0]
    if not candidates:
        print(f"Error: {label} not found in any active account.", file=sys.stderr)
        sys.exit(1)
    if len(candidates) > 1:
        names = ", ".join(acct.name for acct in candidates)
        print(f"Error: {label} exists in multiple accounts ({names}). "
              f"Use --account to specify which one.", file=sys.stderr)
        sys.exit(1)
    return candidates[0]


# ── Extension commands ───────────────────────────────────────────────────────

def cmd_accounts(a):
    accounts = discover_accounts(force_refresh=True)
    if getattr(a, "json", False):
        print(json.dumps([
            {"name": acct.name, "type": acct.type, "storePath": str(acct.store_path),
             "default": index == 0}
            for index, acct in enumerate(accounts)
        ], indent=2, ensure_ascii=False))
        return
    if not accounts:
        print("No Reminders accounts found.")
        return
    print(core.C.bold("Reminders Accounts:"))
    for index, acct in enumerate(accounts):
        default = core.C.dim(" (default)") if index == 0 else ""
        print(f"  {core.safe_display(acct.name)}{default}  {core.C.dim(acct.type)}")
    print(f"\n{len(accounts)} account{'s' if len(accounts) != 1 else ''}")


CONFIG_KEYS = ("accountScope", "storeDir", "dbPath")


def cmd_config(a):
    config = load_config()
    key = getattr(a, "key", None)
    value = getattr(a, "value", None)

    if key and key not in CONFIG_KEYS:
        print(f"Error: unknown config key {key!r}. Supported: {', '.join(CONFIG_KEYS)}",
              file=sys.stderr)
        sys.exit(1)

    if key and value is not None:
        if value == "":
            config.pop(key, None)
        else:
            config[key] = value
        save_config(config)

    if getattr(a, "json", False):
        print(json.dumps(config if not key else {key: config.get(key, "")},
                         indent=2, ensure_ascii=False))
        return
    if key:
        print(config.get(key, ""))
        return
    if not config:
        print("No configuration set.")
        return
    print(core.C.bold("RemCTL Configuration:"))
    for name in CONFIG_KEYS:
        if name in config:
            print(f"  {name}: {config[name]}")


# ── Integration hooks ────────────────────────────────────────────────────────

def _add_scope_flags(parser, *, all_accounts=True):
    import argparse
    parser.add_argument("--account", dest="account", metavar="NAME",
                        default=argparse.SUPPRESS,
                        help="Act on a specific Reminders account "
                             "(see `remctl accounts`)")
    if all_accounts:
        parser.add_argument("--all-accounts", dest="all_accounts",
                            action="store_true", default=argparse.SUPPRESS,
                            help="Act on every connected Reminders account")


def _patch_command_token_scanner():
    """Teach core's command-token scanner that --account takes a value.

    Without this `remctl --account NAME lists` would read NAME as the command.
    Wrapping keeps the fix local to this module.
    """
    original = core.first_command_token

    def scanner(args):
        filtered, skip = [], False
        for token in args:
            if skip:
                skip = False
                continue
            if token == "--account":
                skip = True
                continue
            if token.startswith("--account=") or token == "--all-accounts":
                continue
            filtered.append(token)
        return original(filtered)

    core.first_command_token = scanner


def register_cli(p, sub):
    """Add multi-account flags and the `accounts`/`config` subcommands."""
    _add_scope_flags(p)
    _patch_command_token_scanner()

    for name in sorted(AGGREGATE_COMMANDS | REMINDER_TARGET_COMMANDS | LIST_TARGET_COMMANDS):
        parser = sub.choices.get(name)
        if parser is None:
            continue
        # On aggregate commands --all-accounts means "read from every account".
        # On single-target commands it means "search every account to resolve
        # the target". SINGLE_ACCOUNT_COMMANDS opt out entirely: their output
        # is per-account by nature, so the flag could only mislead.
        _add_scope_flags(parser, all_accounts=name not in SINGLE_ACCOUNT_COMMANDS)

    c = sub.add_parser("accounts", help="List connected Reminders accounts")
    c.add_argument("--json", action="store_true", help="Output JSON")

    c = sub.add_parser(
        "config",
        help="Get or set remctl configuration",
        description="Get or set persistent remctl configuration values.",
        epilog=("Supported keys:\n"
                "  accountScope   Default scope: 'all', an account name, or '' to reset\n"
                "  storeDir       Override the Reminders Stores directory\n"
                "  dbPath         Pin a specific Reminders store file\n"),
    )
    c.add_argument("key", nargs="?", help="Config key")
    c.add_argument("value", nargs="?", help="Value to set; pass '' to clear")
    c.add_argument("--json", action="store_true", help="Output JSON")


def _reject_unsupported_flags(a, sub):
    """Match core's behavior: never silently ignore a scope flag."""
    parser = sub.choices.get(getattr(a, "cmd", None))
    options = set()
    if parser:
        for action in parser._actions:
            options.update(action.option_strings)
    label = getattr(a, "cmd", None) or "(none)"
    if _requested_account_names(a) and "--account" not in options:
        print(f"{core.CLI_NAME}: error: command {label!r} does not support --account",
              file=sys.stderr)
        sys.exit(2)
    if getattr(a, "all_accounts", False) and "--all-accounts" not in options:
        print(f"{core.CLI_NAME}: error: command {label!r} does not support --all-accounts",
              file=sys.stderr)
        sys.exit(2)


def install(cmds, a, sub):
    """Wrap the core dispatch table with account-aware handlers.

    Returns `cmds` unchanged when the user has not opted in, so the default
    single-account path is exactly core's.
    """
    cmds = dict(cmds)
    cmds["accounts"] = cmd_accounts
    cmds["config"] = cmd_config

    _reject_unsupported_flags(a, sub)

    command = getattr(a, "cmd", None)
    if command in ("accounts", "config") or not is_multi_account_mode(a):
        return cmds

    handler = cmds.get(command)
    if handler is None:
        return cmds

    scope = resolve_account_scope(a)
    if not scope:
        return cmds

    if command in AGGREGATE_COMMANDS and len(scope) > 1:
        cmds[command] = lambda args, _h=handler, _s=scope: _aggregate(_h, args, _s)
    else:
        def targeted(args, _h=handler, _s=scope, _c=command):
            with account_context(_pick_target_account(args, _s, _c)):
                _h(args)
        cmds[command] = targeted
    return cmds
