"""Tests for the optional multi-account extension (remctl_accounts).

These cover the extension in isolation from core. The companion guarantee --
that core behaves identically with the module absent -- is covered by
tests/test_cli.py, which is upstream's suite and is never modified here.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import load_module

import remctl_accounts


def _load_core():
    core = load_module("remctl_accounts_core_test", "remctl")
    return core


class AccountsTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = _load_core()
        cls.mod = remctl_accounts

    def setUp(self):
        # Isolate from the developer's real config and environment.
        self._tmp = tempfile.TemporaryDirectory()
        self._config_dir = Path(self._tmp.name)
        self._saved_env = {
            key: os.environ.pop(key, None)
            for key in ("REMCTL_ACCOUNT_SCOPE", "REMCTL_DB", "REMCTL_STORE_DIR")
        }
        self._saved_config_dir = self.core.CONFIG_DIR
        self.core.CONFIG_DIR = self._config_dir
        # Point the extension at this test's core instance.
        self.mod.bind(self.core.__dict__)
        self.mod._ACCOUNT_CACHE = None

    def tearDown(self):
        self.core.CONFIG_DIR = self._saved_config_dir
        self.mod._ACCOUNT_CACHE = None
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────
    def _account(self, name, acct_type="iCloud", path=None):
        return self.mod.Account(Path(path or f"/tmp/{name}.sqlite"), name, acct_type)

    def _args(self, **kw):
        kw.setdefault("json", False)
        kw.setdefault("format", None)
        return SimpleNamespace(**kw)


class ConfigTests(AccountsTestBase):
    def test_load_config_returns_empty_when_missing(self):
        self.assertEqual(self.mod.load_config(), {})

    def test_save_then_load_roundtrips(self):
        self.mod.save_config({"accountScope": "all"})
        self.assertEqual(self.mod.load_config(), {"accountScope": "all"})

    def test_load_config_tolerates_invalid_json(self):
        self.mod.config_file().write_text("{not json")
        self.assertEqual(self.mod.load_config(), {})

    def test_load_config_rejects_non_dict_payload(self):
        self.mod.config_file().write_text("[1, 2, 3]")
        self.assertEqual(self.mod.load_config(), {})

    def test_cmd_config_rejects_unknown_key(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            self.mod.cmd_config(self._args(key="bogus", value="x"))
        self.assertIn("unknown config key", err.getvalue())

    def test_cmd_config_sets_and_clears_key(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.mod.cmd_config(self._args(key="accountScope", value="all"))
        self.assertEqual(self.mod.load_config().get("accountScope"), "all")
        with contextlib.redirect_stdout(io.StringIO()):
            self.mod.cmd_config(self._args(key="accountScope", value=""))
        self.assertNotIn("accountScope", self.mod.load_config())

    def test_store_dir_and_db_override_prefer_env_over_config(self):
        self.mod.save_config({"storeDir": "/from/config", "dbPath": "/from/config.sqlite"})
        self.assertEqual(self.mod.store_dir(), Path("/from/config"))
        os.environ["REMCTL_STORE_DIR"] = "/from/env"
        os.environ["REMCTL_DB"] = "/from/env.sqlite"
        self.assertEqual(self.mod.store_dir(), Path("/from/env"))
        self.assertEqual(self.mod.db_override(), Path("/from/env.sqlite"))


class AccountTypeDetectionTests(AccountsTestBase):
    def test_exchange_identifier_detected(self):
        self.assertEqual(
            self.mod._account_type_from_identifiers(
                ["UUID/com.apple.exchangesync.exchangesyncd"]), "Exchange")

    def test_icloud_identifier_detected(self):
        self.assertEqual(
            self.mod._account_type_from_identifiers(
                ["UUID/com.apple.reminders.sharingextension"]), "iCloud")

    def test_generic_caldav_identifier_detected(self):
        self.assertEqual(
            self.mod._account_type_from_identifiers(["UUID/org.example.caldav"]),
            "CalDAV")

    def test_unknown_identifier_falls_back_to_local(self):
        self.assertEqual(self.mod._account_type_from_identifiers([]), "Local")

    def test_bridge_source_type_refines_heuristic(self):
        """A concrete EventKit label (Exchange, ...) wins."""
        store = Path("/tmp/data-work.sqlite")
        with (
            mock.patch.object(self.core, "reminders_store_access_error", return_value=None),
            mock.patch.object(self.mod, "db_override", return_value=None),
            mock.patch.object(self.mod, "store_dir",
                              return_value=SimpleNamespace(glob=lambda _p: [store])),
            mock.patch.object(self.mod, "_store_account_info",
                              return_value=("Work", "Local")),
            mock.patch.object(self.mod, "_bridge_source_types",
                              return_value={"Work": "Exchange"}),
            mock.patch.object(self.core, "reminders_db_score", return_value=(1,)),
        ):
            accounts = self.mod.discover_accounts(force_refresh=True)
        self.assertEqual([(a.name, a.type) for a in accounts], [("Work", "Exchange")])


class StoreAccountInfoTests(AccountsTestBase):
    """_store_account_info against minimal Reminders-shaped SQLite stores."""

    def _store(self, accounts=(), identifiers=()):
        path = self._config_dir / f"Data-{len(list(self._config_dir.glob('*.sqlite')))}.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT);
            INSERT INTO Z_PRIMARYKEY VALUES (7, 'REMCDAccount');
            CREATE TABLE ZREMCDOBJECT (Z_ENT INTEGER, ZNAME TEXT, ZTYPE INTEGER,
                                       ZMARKEDFORDELETION INTEGER);
            CREATE TABLE ZREMCDREPLICAMANAGER (ZIDENTIFIER TEXT);
        """)
        conn.executemany("INSERT INTO ZREMCDOBJECT VALUES (7, ?, ?, ?)", accounts)
        conn.executemany("INSERT INTO ZREMCDREPLICAMANAGER VALUES (?)", [(i,) for i in identifiers])
        conn.commit()
        conn.close()
        return path

    def test_account_type_comes_from_the_account_record(self):
        for code, expected in ((1, "Local"), (2, "iCloud"), (3, "CalDAV"), (4, "Exchange")):
            path = self._store(accounts=[("Acct", code, 0)])
            self.assertEqual(self.mod._store_account_info(path), ("Acct", expected))

    def test_unsynced_caldav_account_is_not_reported_as_local(self):
        """No lists or replica records yet, but the account record says CalDAV."""
        path = self._store(accounts=[("RemCTL CalDAV Test", 3, 0)])
        self.assertEqual(self.mod._store_account_info(path), ("RemCTL CalDAV Test", "CalDAV"))

    def test_store_without_an_account_record_is_skipped(self):
        """Reminders leaves empty stores behind when an account is re-added."""
        self.assertIsNone(self.mod._store_account_info(self._store()))

    def test_account_marked_for_deletion_is_skipped(self):
        path = self._store(accounts=[("Gone", 3, 1)])
        self.assertIsNone(self.mod._store_account_info(path))

    def test_unknown_type_code_falls_back_to_identifier_heuristic(self):
        path = self._store(accounts=[("Work", 99, 0)],
                           identifiers=["UUID/com.apple.exchangesync.exchangesyncd"])
        self.assertEqual(self.mod._store_account_info(path), ("Work", "Exchange"))

    def test_unnamed_account_gets_a_typed_placeholder_name(self):
        path = self._store(accounts=[(None, 3, 0)])
        info = self.mod._store_account_info(path)
        assert info is not None
        name, acct_type = info
        self.assertEqual(acct_type, "CalDAV")
        self.assertTrue(name.startswith("CalDAV ("))


class DiscoveryTests(AccountsTestBase):
    def _discover(self, infos, scores=None):
        paths = [Path(f"/tmp/data-{i}.sqlite") for i in range(len(infos))]
        info_map = dict(zip(paths, infos))
        score_map = dict(zip(paths, scores or [(i,) for i in range(len(infos), 0, -1)]))
        with (
            mock.patch.object(self.core, "reminders_store_access_error", return_value=None),
            mock.patch.object(self.mod, "db_override", return_value=None),
            mock.patch.object(self.mod, "store_dir",
                              return_value=SimpleNamespace(glob=lambda _p: list(paths))),
            mock.patch.object(self.mod, "_store_account_info",
                              side_effect=lambda p: info_map.get(p)),
            mock.patch.object(self.mod, "_bridge_source_types", return_value={}),
            mock.patch.object(self.core, "reminders_db_score",
                              side_effect=lambda p: score_map[p]),
        ):
            return self.mod.discover_accounts(force_refresh=True)

    def test_hidden_internal_account_is_excluded(self):
        accounts = self._discover([("iCloud", "iCloud"), ("LocalInternal", "Local")])
        self.assertEqual([a.name for a in accounts], ["iCloud"])

    def test_unreadable_store_is_skipped(self):
        accounts = self._discover([("iCloud", "iCloud"), None])
        self.assertEqual([a.name for a in accounts], ["iCloud"])

    def test_accounts_are_ranked_by_core_scorer(self):
        """accounts[0] must match what core find_main_db_path() would choose."""
        accounts = self._discover(
            [("Low", "Exchange"), ("High", "iCloud")],
            scores=[(1,), (9,)],
        )
        self.assertEqual([a.name for a in accounts], ["High", "Low"])

    def test_db_override_pins_a_single_account(self):
        pinned = Path("/tmp/pinned.sqlite")
        with (
            mock.patch.object(self.core, "reminders_store_access_error", return_value=None),
            mock.patch.object(self.mod, "db_override", return_value=pinned),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch.object(self.mod, "_store_account_info",
                              return_value=("Pinned", "Exchange")),
        ):
            accounts = self.mod.discover_accounts(force_refresh=True)
        self.assertEqual([(a.name, str(a.store_path)) for a in accounts],
                         [("Pinned", str(pinned))])


class ScopeResolutionTests(AccountsTestBase):
    def setUp(self):
        super().setUp()
        self.all = [self._account("iCloud"), self._account("Work", "Exchange"),
                    self._account("Fastmail", "CalDAV")]
        self._patch = mock.patch.object(self.mod, "discover_accounts", return_value=self.all)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_default_scope_is_single_first_account(self):
        self.assertEqual([a.name for a in self.mod.resolve_account_scope(self._args())],
                         ["iCloud"])

    def test_all_accounts_flag(self):
        scope = self.mod.resolve_account_scope(self._args(all_accounts=True))
        self.assertEqual([a.name for a in scope], ["iCloud", "Work", "Fastmail"])

    def test_named_account_is_case_insensitive(self):
        scope = self.mod.resolve_account_scope(self._args(account="fastmail"))
        self.assertEqual([a.name for a in scope], ["Fastmail"])

    def test_unknown_account_exits_with_available_list(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            self.mod.resolve_account_scope(self._args(account="Nope"))
        self.assertIn("unknown account(s): Nope", err.getvalue())
        self.assertIn("Fastmail", err.getvalue())

    def test_env_scope_all(self):
        os.environ["REMCTL_ACCOUNT_SCOPE"] = "all"
        self.assertEqual(len(self.mod.resolve_account_scope(self._args())), 3)

    def test_config_scope_named_account(self):
        self.mod.save_config({"accountScope": "Work"})
        self.assertEqual([a.name for a in self.mod.resolve_account_scope(self._args())],
                         ["Work"])

    def test_flag_outranks_env_and_config(self):
        os.environ["REMCTL_ACCOUNT_SCOPE"] = "all"
        self.mod.save_config({"accountScope": "all"})
        scope = self.mod.resolve_account_scope(self._args(account="Work"))
        self.assertEqual([a.name for a in scope], ["Work"])

    def test_is_multi_account_mode_requires_opt_in(self):
        self.assertFalse(self.mod.is_multi_account_mode(self._args()))
        self.assertTrue(self.mod.is_multi_account_mode(self._args(all_accounts=True)))
        self.assertTrue(self.mod.is_multi_account_mode(self._args(account="Work")))
        os.environ["REMCTL_ACCOUNT_SCOPE"] = "all"
        self.assertTrue(self.mod.is_multi_account_mode(self._args()))


class AccountContextTests(AccountsTestBase):
    def test_binds_and_restores_db_opener(self):
        account = self._account("Work", "Exchange")
        original = self.core._db_opener
        with mock.patch.object(self.mod.sqlite3, "connect", return_value=mock.Mock()):
            with self.mod.account_context(account):
                self.assertIsNotNone(self.core._db_opener)
                self.assertIsNot(self.core._db_opener, original)
        self.assertIs(self.core._db_opener, original)

    def test_bridge_write_payloads_get_account_hint(self):
        account = self._account("Work", "Exchange")
        seen = []
        with mock.patch.object(self.core, "bridge_call", side_effect=lambda d: seen.append(d)):
            with self.mod.account_context(account):
                self.core.bridge_call({"action": "create", "title": "T"})
        self.assertEqual(seen[0]["account"], "Work")

    def test_bridge_read_payloads_are_untouched(self):
        account = self._account("Work", "Exchange")
        seen = []
        with mock.patch.object(self.core, "bridge_call", side_effect=lambda d: seen.append(d)):
            with self.mod.account_context(account):
                self.core.bridge_call({"action": "list_calendars"})
        self.assertNotIn("account", seen[0])

    def test_explicit_account_in_payload_is_not_overwritten(self):
        account = self._account("Work", "Exchange")
        seen = []
        with mock.patch.object(self.core, "bridge_call", side_effect=lambda d: seen.append(d)):
            with self.mod.account_context(account):
                self.core.bridge_call({"action": "create", "account": "Chosen"})
        self.assertEqual(seen[0]["account"], "Chosen")

    def test_bridge_functions_restored_after_exit(self):
        account = self._account("Work")
        original = self.core.bridge_call
        with self.mod.account_context(account):
            pass
        self.assertIs(self.core.bridge_call, original)


class JsonMergeTests(AccountsTestBase):
    def test_lists_are_concatenated_and_tagged(self):
        merged = self.mod._merge_json([
            (self._account("iCloud"), json.dumps([{"id": 1}])),
            (self._account("Work", "Exchange"), json.dumps([{"id": 2}])),
        ])
        self.assertEqual([m["id"] for m in merged], [1, 2])
        self.assertEqual(merged[1]["account"], "Work")
        self.assertEqual(merged[1]["accountType"], "Exchange")

    def test_nested_children_are_tagged(self):
        merged = self.mod._merge_json([
            (self._account("iCloud"), json.dumps([{"id": 1, "children": [{"id": 2}]}])),
        ])
        self.assertEqual(merged[0]["children"][0]["account"], "iCloud")

    def test_object_payloads_are_keyed_by_account_with_totals(self):
        merged = self.mod._merge_json([
            (self._account("iCloud"), json.dumps({"total": 2, "active": 1})),
            (self._account("Work", "Exchange"), json.dumps({"total": 3, "active": 2})),
        ])
        self.assertEqual(merged["total"]["total"], 5)
        self.assertEqual(merged["total"]["active"], 3)
        self.assertIn("Work", merged["accounts"])

    def test_unparseable_output_is_skipped(self):
        merged = self.mod._merge_json([(self._account("iCloud"), "not json")])
        self.assertEqual(merged, [])


class TargetResolutionTests(AccountsTestBase):
    def test_single_account_scope_needs_no_lookup(self):
        only = self._account("iCloud")
        self.assertIs(self.mod._pick_target_account(self._args(id=1), [only], "done"), only)

    def test_ambiguous_reminder_id_exits_with_guidance(self):
        scope = [self._account("A"), self._account("B")]
        with (
            mock.patch.object(self.mod, "_accounts_holding_reminder", return_value=scope),
            contextlib.redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit),
        ):
            self.mod._pick_target_account(self._args(id=42), scope, "done")
        self.assertIn("multiple accounts", err.getvalue())
        self.assertIn("--account", err.getvalue())

    def test_missing_reminder_id_exits(self):
        scope = [self._account("A"), self._account("B")]
        with (
            mock.patch.object(self.mod, "_accounts_holding_reminder", return_value=[]),
            contextlib.redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit),
        ):
            self.mod._pick_target_account(self._args(id=42), scope, "done")
        self.assertIn("not found in any active account", err.getvalue())

    def test_unique_holder_is_selected(self):
        scope = [self._account("A"), self._account("B")]
        with mock.patch.object(self.mod, "_accounts_holding_reminder",
                               return_value=[scope[1]]):
            picked = self.mod._pick_target_account(self._args(id=42), scope, "done")
        self.assertIs(picked, scope[1])

    def test_list_target_resolves_by_list_name(self):
        scope = [self._account("A"), self._account("B")]
        with mock.patch.object(self.mod, "_accounts_holding_list",
                               return_value=[scope[0]]) as lookup:
            picked = self.mod._pick_target_account(
                self._args(list="Projects", list_id=None), scope, "add")
        self.assertIs(picked, scope[0])
        lookup.assert_called_once()


class FlagSupportTests(AccountsTestBase):
    """Flags on Exchange/CalDAV are refused up front instead of falsely succeeding."""

    def _run(self, command, acct_type, **kw):
        handler = mock.Mock()
        args = self._args(cmd=command, account="Acct", **kw)
        sub = SimpleNamespace(choices={command: SimpleNamespace(
            _actions=[SimpleNamespace(option_strings=["--account"])])})
        with mock.patch.object(self.mod, "resolve_account_scope",
                               return_value=[self._account("Acct", acct_type)]), \
                mock.patch.object(self.mod.sqlite3, "connect", return_value=mock.Mock()):
            cmds = self.mod.install({command: handler}, args, sub)
            cmds[command](args)
        return handler

    def test_flag_is_refused_on_exchange_and_caldav(self):
        for acct_type in ("Exchange", "CalDAV"):
            for command in ("flag", "unflag"):
                with self.subTest(command=command, acct_type=acct_type), \
                        contextlib.redirect_stderr(io.StringIO()) as err, \
                        self.assertRaises(SystemExit) as raised:
                    self._run(command, acct_type, id=2)
                self.assertEqual(raised.exception.code, 1)
                self.assertIn("no flag attribute", err.getvalue())

    def test_json_refusal_has_a_stable_code(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            self._run("flag", "CalDAV", id=2, json=True)
        self.assertEqual(json.loads(err.getvalue())["code"], "flag_unsupported_for_account")

    def test_add_with_flag_is_refused_before_creating(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self._run("add", "Exchange", flag=True)

    def test_add_without_flag_and_icloud_flag_still_run(self):
        self._run("add", "CalDAV", flag=False).assert_called_once()
        self._run("flag", "iCloud", id=2).assert_called_once()
        self._run("flag", "Local", id=2).assert_called_once()


class InstallTests(AccountsTestBase):
    def setUp(self):
        super().setUp()
        self.sub = SimpleNamespace(choices={})

    def _sub_with(self, name, options=("--account", "--all-accounts")):
        actions = [SimpleNamespace(option_strings=list(options))]
        return SimpleNamespace(choices={name: SimpleNamespace(_actions=actions)})

    def test_dispatch_table_untouched_without_opt_in(self):
        handler = lambda a: None
        cmds = self.mod.install({"lists": handler}, self._args(cmd="lists"),
                                self._sub_with("lists"))
        self.assertIs(cmds["lists"], handler)

    def test_extension_commands_always_registered(self):
        cmds = self.mod.install({}, self._args(cmd=None), self.sub)
        self.assertIs(cmds["accounts"], self.mod.cmd_accounts)
        self.assertIs(cmds["config"], self.mod.cmd_config)

    def test_aggregate_command_is_wrapped_when_scope_spans_accounts(self):
        handler = lambda a: None
        scope = [self._account("A"), self._account("B")]
        with mock.patch.object(self.mod, "resolve_account_scope", return_value=scope):
            cmds = self.mod.install({"lists": handler},
                                    self._args(cmd="lists", all_accounts=True),
                                    self._sub_with("lists"))
        self.assertIsNot(cmds["lists"], handler)

    def test_single_account_scope_uses_targeted_wrapper(self):
        handler = lambda a: None
        with mock.patch.object(self.mod, "resolve_account_scope",
                               return_value=[self._account("Work", "Exchange")]):
            cmds = self.mod.install({"done": handler},
                                    self._args(cmd="done", id=1, account="Work"),
                                    self._sub_with("done"))
        self.assertIsNot(cmds["done"], handler)

    def test_account_flag_rejected_for_unsupported_command(self):
        sub = self._sub_with("export", options=("--json",))
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
            self.mod.install({"export": lambda a: None},
                             self._args(cmd="export", account="Work"), sub)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("does not support --account", err.getvalue())

    def test_single_account_commands_do_not_offer_all_accounts(self):
        """export/import cannot merge across accounts, so the flag is withheld."""
        self.assertIn("export", self.mod.SINGLE_ACCOUNT_COMMANDS)
        self.assertIn("import", self.mod.SINGLE_ACCOUNT_COMMANDS)
        self.assertNotIn("show", self.mod.SINGLE_ACCOUNT_COMMANDS)
        self.assertNotIn("done", self.mod.SINGLE_ACCOUNT_COMMANDS)

    def test_all_accounts_flag_rejected_for_unsupported_command(self):
        sub = self._sub_with("export", options=("--account",))
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            self.mod.install({"export": lambda a: None},
                             self._args(cmd="export", all_accounts=True), sub)
        self.assertIn("does not support --all-accounts", err.getvalue())


class AggregateOutputTests(AccountsTestBase):
    def _ok(self, text):
        return self.mod.CaptureResult(text, "", True)

    def test_human_output_gets_per_account_headers(self):
        scope = [self._account("iCloud"), self._account("Work", "Exchange")]
        outputs = {"iCloud": "one\n", "Work": "two\n"}

        def handler(a):
            pass

        def fake_capture(_h, _a, account):
            return self._ok(outputs[account.name])

        with mock.patch.object(self.mod, "_run_capture", side_effect=fake_capture):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.mod._aggregate(handler, self._args(), scope)
        text = out.getvalue()
        self.assertIn("iCloud", text)
        self.assertIn("Work", text)
        self.assertIn("one", text)
        self.assertIn("two", text)

    def test_single_account_scope_has_no_header(self):
        scope = [self._account("iCloud")]
        with mock.patch.object(self.mod, "_run_capture", return_value=self._ok("body\n")):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.mod._aggregate(lambda a: None, self._args(), scope)
        self.assertEqual(out.getvalue().strip(), "body")

    def test_json_mode_emits_merged_document(self):
        scope = [self._account("iCloud"), self._account("Work", "Exchange")]
        payloads = {"iCloud": json.dumps([{"id": 1}]), "Work": json.dumps([{"id": 2}])}
        with mock.patch.object(self.mod, "_run_capture",
                               side_effect=lambda _h, _a, acct: self._ok(payloads[acct.name])):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.mod._aggregate(lambda a: None, self._args(json=True), scope)
        merged = json.loads(out.getvalue())
        self.assertEqual([m["id"] for m in merged], [1, 2])
        self.assertEqual({m["account"] for m in merged}, {"iCloud", "Work"})

    def test_account_that_errors_is_skipped_not_fatal(self):
        scope = [self._account("iCloud"), self._account("Broken", "Exchange")]

        def capture(_h, _a, account):
            if account.name == "Broken":
                return self.mod.CaptureResult("", "boom\n", False)
            return self._ok("ok\n")

        with mock.patch.object(self.mod, "_run_capture", side_effect=capture):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.mod._aggregate(lambda a: None, self._args(), scope)
        self.assertIn("ok", out.getvalue())
        self.assertNotIn("Broken", out.getvalue())


class CommandClassificationTests(AccountsTestBase):
    """Reads aggregate; commands that act on one thing refuse an ambiguous name."""

    def test_reads_aggregate_across_accounts(self):
        for name in ("show", "sections", "sharees", "lists", "search", "today"):
            self.assertIn(name, self.mod.AGGREGATE_COMMANDS, name)

    def test_mutations_stay_single_target(self):
        for name in ("add", "list-edit", "list-delete", "section-create"):
            self.assertIn(name, self.mod.LIST_TARGET_COMMANDS, name)
            self.assertNotIn(name, self.mod.AGGREGATE_COMMANDS, name)

    def test_reminder_mutations_stay_single_target(self):
        for name in ("done", "undone", "edit", "delete", "flag", "unflag"):
            self.assertIn(name, self.mod.REMINDER_TARGET_COMMANDS, name)
            self.assertNotIn(name, self.mod.AGGREGATE_COMMANDS, name)

    def test_no_command_is_both_aggregate_and_targeted(self):
        targeted = self.mod.REMINDER_TARGET_COMMANDS | self.mod.LIST_TARGET_COMMANDS
        self.assertEqual(self.mod.AGGREGATE_COMMANDS & targeted, set())

    def test_every_classified_name_is_a_real_command(self):
        """Guards against typos like "section" for the real "sections"."""
        parser, sub = self.core.build_parser()
        known = set(sub.choices)
        classified = (self.mod.AGGREGATE_COMMANDS
                      | self.mod.REMINDER_TARGET_COMMANDS
                      | self.mod.LIST_TARGET_COMMANDS)
        self.assertEqual(classified - known, set())


class AggregateErrorHandlingTests(AccountsTestBase):
    def _result(self, out="", err="", ok=True):
        return self.mod.CaptureResult(out, err, ok)

    def test_missing_in_some_accounts_does_not_leak_errors(self):
        """`show X --all-accounts` must not print "not found" for accounts
        that simply do not have list X."""
        scope = [self._account("Has"), self._account("Missing", "Exchange")]

        def capture(_h, _a, account):
            if account.name == "Missing":
                return self._result(err="Error: list not found: X\n", ok=False)
            return self._result(out="X:\n[ ] #1 item\n")

        with mock.patch.object(self.mod, "_run_capture", side_effect=capture):
            with contextlib.redirect_stdout(io.StringIO()) as out, \
                 contextlib.redirect_stderr(io.StringIO()) as err:
                self.mod._aggregate(lambda a: None, self._args(), scope)
        self.assertIn("#1 item", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_missing_in_every_account_surfaces_the_error(self):
        scope = [self._account("A"), self._account("B", "Exchange")]
        with mock.patch.object(
            self.mod, "_run_capture",
            return_value=self._result(err="Error: list not found: X\n", ok=False),
        ):
            with contextlib.redirect_stderr(io.StringIO()) as err, \
                 self.assertRaises(SystemExit) as raised:
                self.mod._aggregate(lambda a: None, self._args(), scope)
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("list not found", err.getvalue())


class RunCaptureTests(AccountsTestBase):
    def _capture(self, handler):
        with mock.patch.object(self.mod.sqlite3, "connect", return_value=mock.Mock()):
            return self.mod._run_capture(handler, self._args(), self._account("A"))

    def test_nonzero_exit_is_marked_not_ok(self):
        def handler(a):
            print("partial")
            sys.exit(1)

        self.assertFalse(self._capture(handler).ok)

    def test_clean_exit_zero_keeps_output(self):
        def handler(a):
            print("done")

        result = self._capture(handler)
        self.assertTrue(result.ok)
        self.assertEqual(result.out, "done\n")

    def test_stderr_is_captured_not_leaked(self):
        """An account missing the target must not print to the real stderr."""
        def handler(a):
            print("nope", file=sys.stderr)
            sys.exit(1)

        result = self._capture(handler)
        self.assertFalse(result.ok)
        self.assertIn("nope", result.err)

    def test_unavailable_database_is_not_fatal(self):
        def handler(a):
            raise self.core.RemindersDBUnavailable("nope")

        self.assertFalse(self._capture(handler).ok)


class CommandTokenScannerTests(AccountsTestBase):
    def test_account_value_is_not_mistaken_for_command(self):
        """`remctl --account NAME lists` must resolve to the `lists` command."""
        original = self.core.first_command_token
        try:
            self.mod._patch_command_token_scanner()
            self.assertEqual(
                self.core.first_command_token(["--account", "Work", "lists"]), "lists")
            self.assertEqual(
                self.core.first_command_token(["--account=Work", "lists"]), "lists")
            self.assertEqual(
                self.core.first_command_token(["--all-accounts", "today"]), "today")
        finally:
            self.core.first_command_token = original


class AccountsCommandTests(AccountsTestBase):
    def test_json_output_marks_the_default_account(self):
        accounts = [self._account("iCloud"), self._account("Work", "Exchange")]
        with mock.patch.object(self.mod, "discover_accounts", return_value=accounts):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.mod.cmd_accounts(self._args(json=True))
        payload = json.loads(out.getvalue())
        self.assertTrue(payload[0]["default"])
        self.assertFalse(payload[1]["default"])
        self.assertEqual(payload[1]["type"], "Exchange")

    def test_human_output_lists_every_account_type(self):
        accounts = [self._account("iCloud"), self._account("Fastmail", "CalDAV"),
                    self._account("Work", "Exchange")]
        with mock.patch.object(self.mod, "discover_accounts", return_value=accounts):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.mod.cmd_accounts(self._args(json=False))
        text = out.getvalue()
        for name in ("iCloud", "Fastmail", "Work"):
            self.assertIn(name, text)
        self.assertIn("3 accounts", text)

    def test_no_accounts_message(self):
        with mock.patch.object(self.mod, "discover_accounts", return_value=[]):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.mod.cmd_accounts(self._args(json=False))
        self.assertIn("No Reminders accounts found", out.getvalue())


class BridgePayloadTests(AccountsTestBase):
    """core.bridge_call()/bridge_call_result() cannot return list payloads."""

    def _result(self, stdout, payload, returncode=0):
        return {"returncode": returncode, "stdout": stdout, "stderr": "", "payload": payload}

    def test_list_payload_recovered_from_stdout(self):
        """list_calendars returns a JSON list, which core coerces to an error dict."""
        raw = '[{"title": "Projects", "calendarIdentifier": "CAL-1"}]'
        coerced = {"status": "error", "message": "..."}
        with (
            mock.patch.object(self.core, "bridge_available", return_value=True),
            mock.patch.object(self.core, "bridge_call_result",
                              return_value=self._result(raw, coerced)),
        ):
            payload = self.mod._bridge_payload({"action": "list_calendars"})
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["calendarIdentifier"], "CAL-1")

    def test_dict_payload_passes_through(self):
        payload = {"calendarItemIdentifier": "EK-1"}
        with (
            mock.patch.object(self.core, "bridge_available", return_value=True),
            mock.patch.object(self.core, "bridge_call_result",
                              return_value=self._result(json.dumps(payload), payload)),
        ):
            self.assertEqual(
                self.mod._bridge_payload({"action": "find_reminder"}), payload)

    def test_nonzero_returncode_yields_none(self):
        with (
            mock.patch.object(self.core, "bridge_available", return_value=True),
            mock.patch.object(self.core, "bridge_call_result",
                              return_value=self._result("", {}, returncode=1)),
        ):
            self.assertIsNone(self.mod._bridge_payload({"action": "x"}))

    def test_absent_bridge_yields_none(self):
        with mock.patch.object(self.core, "bridge_available", return_value=False):
            self.assertIsNone(self.mod._bridge_payload({"action": "x"}))


class CreatedReminderLookupTests(AccountsTestBase):
    """`add --json` reports numericId for non-iCloud reminders too."""

    def _db(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE ZREMCDBASELIST (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT,
                                         ZMARKEDFORDELETION INTEGER);
            INSERT INTO ZREMCDBASELIST VALUES (1, 'RemCTL Test', 0);
            CREATE TABLE ZREMCDREMINDER (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZLIST INTEGER,
                                         ZCKIDENTIFIER TEXT, ZDACALENDARITEMUNIQUEIDENTIFIER TEXT,
                                         ZMARKEDFORDELETION INTEGER, ZCOMPLETED INTEGER);
            INSERT INTO ZREMCDREMINDER VALUES (3, 'Same title', 1, NULL, 'EK-ID-3', 0, 0);
            INSERT INTO ZREMCDREMINDER VALUES (4, 'Same title', 1, NULL, 'EK-ID-4', 0, 0);
        """)
        return conn

    def _lookup(self, db, identifier):
        with mock.patch.object(self.mod.sqlite3, "connect", return_value=db), \
                self.mod.account_context(self._account("Test", "CalDAV")):
            return self.core.q_reminder_by_identifier(db, identifier)

    def test_bridge_id_resolves_the_exact_row(self):
        db = self._db()
        with mock.patch.object(self.core, "rem_cols", return_value="r.Z_PK AS Z_PK"), \
                mock.patch.object(self.core, "LIVE_REMINDER_SQL", "r.ZMARKEDFORDELETION = 0"):
            self.assertEqual(self._lookup(db, "EK-ID-3")["Z_PK"], 3)
            self.assertEqual(self._lookup(db, "EK-ID-4")["Z_PK"], 4)

    def test_unknown_id_is_not_guessed_from_title(self):
        db = self._db()
        with mock.patch.object(self.core, "rem_cols", return_value="r.Z_PK AS Z_PK"), \
                mock.patch.object(self.core, "LIVE_REMINDER_SQL", "r.ZMARKEDFORDELETION = 0"):
            self.assertIsNone(self._lookup(db, "Same title"))
            self.assertIsNone(self._lookup(db, ""))

    def test_lookup_is_restored_after_the_context(self):
        original = self.core.q_reminder_by_identifier
        with mock.patch.object(self.mod.sqlite3, "connect", return_value=mock.Mock()), \
                self.mod.account_context(self._account("Test", "CalDAV")):
            self.assertIsNot(self.core.q_reminder_by_identifier, original)
        self.assertIs(self.core.q_reminder_by_identifier, original)


class IdentifierBackfillTests(AccountsTestBase):
    """Exchange/CalDAV reminders have no ZCKIDENTIFIER; core refuses to touch
    them. The extension resolves the real EventKit id so core's own bridge
    path works unchanged."""

    def setUp(self):
        super().setUp()
        self.account = self._account("Work", "Exchange")

    def test_existing_identifier_is_left_alone(self):
        row = {"ZCKIDENTIFIER": "CK-1", "ZTITLE": "T", "list_name": "Projects"}
        self.assertIs(self.mod._with_identifier(row, self.account), row)

    def test_missing_identifier_is_resolved_via_eventkit(self):
        """No stored EventKit id: fall back to (calendar, title) for a unique title."""
        row = {"ZCKIDENTIFIER": None, "ZTITLE": "T", "list_name": "Projects"}
        with (
            mock.patch.object(self.mod, "_store_identity", return_value=(None, True)),
            mock.patch.object(self.mod, "_calendar_id_for", return_value="CAL-1"),
            mock.patch.object(self.mod, "_bridge_payload",
                              return_value={"calendarItemIdentifier": "EK-9"}),
        ):
            out = self.mod._with_identifier(row, self.account)
        self.assertEqual(out["ZCKIDENTIFIER"], "EK-9")
        self.assertEqual(out["ZTITLE"], "T")

    def test_unresolvable_reminder_is_returned_unchanged(self):
        row = {"ZCKIDENTIFIER": None, "ZTITLE": "T", "list_name": "Projects"}
        with (
            mock.patch.object(self.mod, "_store_identity", return_value=(None, True)),
            mock.patch.object(self.mod, "_calendar_id_for", return_value="CAL-1"),
            mock.patch.object(self.mod, "_bridge_payload", return_value=None),
        ):
            self.assertIs(self.mod._with_identifier(row, self.account), row)

    def test_unknown_calendar_returns_row_unchanged(self):
        row = {"ZCKIDENTIFIER": None, "ZTITLE": "T", "list_name": "Projects"}
        with (
            mock.patch.object(self.mod, "_store_identity", return_value=(None, True)),
            mock.patch.object(self.mod, "_calendar_id_for", return_value=None),
        ):
            self.assertIs(self.mod._with_identifier(row, self.account), row)

    def _store(self):
        """Two reminders share a title in one list, as in a real CalDAV test run."""
        path = self._config_dir / "Data-dup.sqlite"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE ZREMCDREMINDER (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZLIST INTEGER,
                                         ZDACALENDARITEMUNIQUEIDENTIFIER TEXT,
                                         ZMARKEDFORDELETION INTEGER);
            INSERT INTO ZREMCDREMINDER VALUES (2, 'Created by remctl', 1, 'EK-2', 0);
            INSERT INTO ZREMCDREMINDER VALUES (4, 'Created by remctl', 1, 'EK-4', 0);
            INSERT INTO ZREMCDREMINDER VALUES (5, 'Created by remctl', 1, NULL, 0);
            INSERT INTO ZREMCDREMINDER VALUES (6, 'Unique', 1, NULL, 0);
            INSERT INTO ZREMCDREMINDER VALUES (7, 'Unique', 1, 'EK-7', 1);
        """)
        conn.commit()
        conn.close()
        return self.mod.Account(path, "Work", "Exchange")

    def test_stored_eventkit_id_targets_the_exact_reminder(self):
        """Editing #4 must not resolve to its same-titled sibling #2."""
        account = self._store()
        row = {"Z_PK": 4, "ZCKIDENTIFIER": None, "ZTITLE": "Created by remctl",
               "list_name": "RemCTL Test"}
        with mock.patch.object(self.mod, "_bridge_payload") as bridge:
            out = self.mod._with_identifier(row, account)
        self.assertEqual(out["ZCKIDENTIFIER"], "EK-4")
        bridge.assert_not_called()

    def test_duplicate_title_without_stored_id_is_refused(self):
        """No exact id and an ambiguous title: leave it for core to refuse."""
        account = self._store()
        row = {"Z_PK": 5, "ZCKIDENTIFIER": None, "ZTITLE": "Created by remctl",
               "list_name": "RemCTL Test"}
        with mock.patch.object(self.mod, "_bridge_payload") as bridge:
            self.assertIs(self.mod._with_identifier(row, account), row)
        bridge.assert_not_called()

    def test_store_identity_reports_id_and_title_uniqueness(self):
        account = self._store()
        self.assertEqual(self.mod._store_identity(account, 4), ("EK-4", False))
        self.assertEqual(self.mod._store_identity(account, 5), (None, False))
        # #7 shares the title but is marked for deletion, so #6 is unique.
        self.assertEqual(self.mod._store_identity(account, 6), (None, True))
        self.assertEqual(self.mod._store_identity(account, 99), (None, False))
        self.assertEqual(self.mod._store_identity(account, None), (None, False))

    def test_none_row_is_passed_through(self):
        self.assertIsNone(self.mod._with_identifier(None, self.account))

    def test_calendar_lookup_matches_on_list_and_account(self):
        calendars = [
            {"title": "Projects", "calendarIdentifier": "CAL-OTHER", "sourceTitle": "iCloud"},
            {"title": "Projects", "calendarIdentifier": "CAL-WORK", "sourceTitle": "Work"},
        ]
        with mock.patch.object(self.mod, "_bridge_payload", return_value=calendars):
            self.assertEqual(self.mod._calendar_id_for("Projects", self.account), "CAL-WORK")

    def test_q_reminder_is_wrapped_inside_account_context(self):
        original = self.core.q_reminder
        with mock.patch.object(self.mod.sqlite3, "connect", return_value=mock.Mock()):
            with self.mod.account_context(self.account):
                self.assertIsNot(self.core.q_reminder, original)
        self.assertIs(self.core.q_reminder, original)


class AccountTypeMergeTests(AccountsTestBase):
    def test_concrete_eventkit_type_wins(self):
        self.assertEqual(self.mod._merge_account_type("Local", "Exchange"), "Exchange")

    def test_generic_caldav_defers_to_store_heuristic(self):
        """EventKit calls iCloud "CalDAV"; the heuristic knows better."""
        self.assertEqual(self.mod._merge_account_type("iCloud", "CalDAV"), "iCloud")

    def test_caldav_heuristic_survives_generic_eventkit_label(self):
        self.assertEqual(self.mod._merge_account_type("CalDAV", "CalDAV"), "CalDAV")

    def test_local_stays_local(self):
        self.assertEqual(self.mod._merge_account_type("Local", "Local"), "Local")

    def test_missing_eventkit_type_keeps_heuristic(self):
        self.assertEqual(self.mod._merge_account_type("Exchange", None), "Exchange")


class CapabilityHostIntegrationTests(AccountsTestBase):
    """The extension must fit upstream's closed Capability Host contracts."""

    def setUp(self):
        super().setUp()
        self._saved_host_env = {
            key: os.environ.pop(key, None)
            for key in ("REMCTL_CAPABILITY_HOST", "REMCTL_CAPABILITY_HOST_ACTIVE", "NO_COLOR")
        }
        self.parser, self.sub = self.core.build_parser()

    def tearDown(self):
        for key, value in self._saved_host_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def test_extension_commands_are_classified_for_the_host(self):
        import remctl_capability_policy
        import remctl_runtime

        self.assertEqual(remctl_runtime.capability_host_command_scope("accounts"), "hosted")
        self.assertEqual(remctl_runtime.capability_host_command_scope("config"), "local")
        # Raises if any parser command is unclassified or stale.
        remctl_capability_policy.validate_command_scope(self.parser)

    def test_extensions_cannot_reclassify_core_commands(self):
        import remctl_runtime

        with self.assertRaises(ValueError):
            remctl_runtime.register_extension_commands(local={"today"})
        with self.assertRaises(ValueError):
            remctl_runtime.register_extension_commands(hosted={"config"})

    def test_building_the_parser_twice_does_not_stack_wrappers(self):
        route = self.core._should_route_capability_host
        scanner = self.core.first_command_token
        self.core.build_parser()
        self.assertIs(self.core._should_route_capability_host, route)
        self.assertIs(self.core.first_command_token, scanner)

    def test_mcp_run_guard_skips_the_account_value(self):
        import remctl_mcp

        self.assertEqual(remctl_mcp._run_command_name(["--account", "Work", "setup"]), "setup")
        self.assertEqual(remctl_mcp._run_command_name(["--all-accounts", "today"]), "today")
        self.assertIn("config", remctl_mcp.RUN_FORBIDDEN_COMMANDS)

    def _parse(self, *argv):
        return self.core.parse_cli_args(self.parser, self.sub, list(argv))

    def test_custom_store_settings_route_direct(self):
        args = self._parse("today")
        with mock.patch.object(self.core.remctl_broker, "should_route", return_value=True):
            self.assertTrue(self.core._should_route_capability_host(args))
            os.environ["REMCTL_DB"] = str(self._config_dir / "pinned.sqlite")
            self.assertFalse(self.core._should_route_capability_host(args))
            os.environ.pop("REMCTL_DB")
            self.mod.save_config({"dbPath": str(self._config_dir / "pinned.sqlite")})
            self.assertFalse(self.core._should_route_capability_host(args))

    def test_custom_store_conflicts_with_force_mode(self):
        args = self._parse("today")
        os.environ["REMCTL_CAPABILITY_HOST"] = "force"
        self.mod.save_config({"storeDir": str(self._config_dir / "stores")})
        with mock.patch.object(self.core.remctl_broker, "should_route", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as raised:
            self.core._should_route_capability_host(args)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("config storeDir", err.getvalue())

    def test_custom_stores_are_ignored_inside_the_host(self):
        self.mod.save_config({"dbPath": str(self._config_dir / "pinned.sqlite"),
                              "storeDir": str(self._config_dir / "stores")})
        os.environ["REMCTL_DB"] = str(self._config_dir / "other.sqlite")
        os.environ["REMCTL_CAPABILITY_HOST_ACTIVE"] = "1"
        self.assertIsNone(self.mod.db_override())
        self.assertEqual(self.mod.store_dir(), self.core.STORE_DIR)

    def test_env_scope_is_forwarded_to_the_host(self):
        os.environ["REMCTL_ACCOUNT_SCOPE"] = "all"
        args = self._parse("today")
        self.assertEqual(
            self.core._normalized_host_argv(["today"], args)[:1], ["--all-accounts"]
        )
        os.environ["REMCTL_ACCOUNT_SCOPE"] = "Exchange"
        self.assertEqual(
            self.core._normalized_host_argv(["today"], args)[:2], ["--account", "Exchange"]
        )

    def test_explicit_scope_flags_win_over_env_scope(self):
        os.environ["REMCTL_ACCOUNT_SCOPE"] = "all"
        argv = ["today", "--account", "Work"]
        args = self._parse(*argv)
        self.assertEqual(self.core._normalized_host_argv(argv, args), argv)

    def test_env_scope_is_not_forwarded_to_unsupported_commands(self):
        os.environ["REMCTL_ACCOUNT_SCOPE"] = "Exchange"
        args = self._parse("doctor")
        self.assertEqual(self.core._normalized_host_argv(["doctor"], args), ["doctor"])

    def test_no_command_host_argv_keeps_scope_flags(self):
        args = self._parse("--account", "Work")
        argv = self.core._no_command_host_argv(args)
        self.assertEqual(argv[:2], ["--account", "Work"])
        self.assertEqual(self._parse(*argv).cmd, "today")


if __name__ == "__main__":
    unittest.main()
