"""Recovery membership, identity preservation, and MCP routing."""
import contextlib
import io
import json
import sqlite3
import unittest
from unittest import mock

from helpers import load_module
import remctl_mcp as mcp

cli = load_module('remctl_recovery_test', 'remctl')
require_private_metadata = cli.require_private_metadata
resolve_required_list_target = cli.resolve_required_list_target_or_die


def run(command, argv):
    args = cli.build_parser()[0].parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            command(args)
        except SystemExit as e:
            code = e.code
    return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()


def identifier(n):
    return f'00000000-0000-4000-8000-{n:012d}'


def native(n, children=()):
    return {'objectID': {'uuid': identifier(n)}, 'subtasks': [native(c) for c in children]}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        cli._REMINDER_COLUMN_CACHE.clear()
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE ZREMCDBASELIST (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT);
            INSERT INTO ZREMCDBASELIST VALUES (10, 'Recovered');
            CREATE TABLE ZREMCDREMINDER (
                Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZNOTES TEXT, ZCOMPLETED INTEGER DEFAULT 0,
                ZFLAGGED INTEGER DEFAULT 0, ZPRIORITY INTEGER DEFAULT 0, ZDUEDATE REAL, ZALLDAY INTEGER,
                ZCOMPLETIONDATE REAL, ZCREATIONDATE REAL, ZPARENTREMINDER INTEGER, ZLIST INTEGER,
                ZICSURL TEXT, ZCKIDENTIFIER TEXT, ZMARKEDFORDELETION INTEGER DEFAULT 1, ZACCOUNT INTEGER DEFAULT 1);
            CREATE TABLE ZREMCDOBJECT (
                Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZREMINDER4 INTEGER, ZMARKEDFORDELETION INTEGER,
                ZFREQUENCY INTEGER, ZINTERVAL INTEGER, ZOCCURRENCECOUNT INTEGER, ZENDDATE REAL,
                ZDAYSOFTHEWEEK BLOB, ZDAYSOFTHEMONTH BLOB, ZMONTHSOFTHEYEAR BLOB, ZDAYSOFTHEYEAR BLOB,
                ZWEEKSOFTHEYEAR BLOB, ZSETPOSITIONS BLOB);
        ''')
        for n in range(1, 5):
            self.db.execute('INSERT INTO ZREMCDREMINDER (Z_PK,ZTITLE,ZNOTES,ZCKIDENTIFIER,ZPARENTREMINDER) VALUES (?,?,?,?,?)',
                            (n, f'Reminder {n}', 'body', identifier(n), 1 if n == 2 else None))
        self.result = {'status': 'ok', 'reminders': [native(1, [2]), native(3)]}
        self.target = {'id': 10, 'title': 'Recovered', 'objectUUID': identifier(10)}
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(cli, 'open_db', return_value=self.db))
        self.helper = self.stack.enter_context(mock.patch.object(cli, 'private_call', return_value=self.result))
        self.stack.enter_context(mock.patch.object(cli, 'resolve_required_list_target_or_die', return_value=self.target))
        self.stack.enter_context(mock.patch.object(cli, 'require_private_metadata'))
        self.stack.enter_context(mock.patch.object(cli, 'private_available', return_value=True))
        self.stack.enter_context(mock.patch.object(cli, '_probe_private_protocol_version', return_value={'ok': True, 'version': 3}))
        self.stack.enter_context(mock.patch.object(cli, '_read_fresh_db_value', side_effect=lambda f: f(self.db)))
        self.stack.enter_context(mock.patch.object(cli.time, 'sleep'))

    def tearDown(self):
        self.stack.close()
        self.db.close()
        cli._REMINDER_COLUMN_CACHE.clear()

    def test_native_membership_excludes_other_tombstones_and_keeps_orphans(self):
        items = cli.deleted_reminders(self.db)
        self.assertEqual([i['id'] for i in items], [1, 3])
        self.assertIsNone(items[0]['list'])
        self.assertEqual(items[0]['subtasks'][0]['restoreId'], 1)
        self.assertTrue(items[0]['recoverable'])
        self.assertEqual(items[0]['notes'], 'body')
        self.assertIsNone(cli.q_reminder(self.db, 1))

    def test_paging_keeps_children_with_parent_and_reports_end(self):
        code, p, _ = run(cli.cmd_deleted, ['deleted', '--limit', '1', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual((p['total'], p['count'], p['nextOffset']), (2, 1, 1))
        self.assertEqual(p['items'][0]['subtasks'][0]['id'], 2)
        _, p, _ = run(cli.cmd_deleted, ['deleted', '--limit', '1', '--offset', '1', '--json'])
        self.assertFalse(p['hasMore'])
        self.assertIsNone(p['nextOffset'])
        code, _, _ = run(cli.cmd_deleted, ['deleted', '--limit', '0', '--json'])
        self.assertEqual(code, 1)

    def test_info_requires_opt_in_and_can_inspect_a_deleted_child(self):
        code, _, _ = run(cli.cmd_info, ['info', '2', '--json'])
        self.assertEqual(code, 1)
        self.helper.assert_not_called()
        code, p, _ = run(cli.cmd_info, ['info', '2', '--include-deleted', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(p['restoreId'], 1)

    def test_unavailable_malformed_and_changed_results_fail_closed(self):
        cases = [{'status': 'error', 'message': 'unavailable'}, {'status': 'ok', 'reminders': [{}]},
                 {'status': 'ok', 'reminders': [native(99)]}, {'status': 'ok', 'reminders': [native(1), {'objectID': {'uuid': 'bad'}}]}]
        for value in cases:
            with self.subTest(value=value):
                self.helper.return_value = value
                code, p, err = run(cli.cmd_deleted, ['deleted', '--json'])
                self.assertEqual(code, 1)
                self.assertIsNone(p)
                self.assertIn('recently_deleted_', err)

    def test_recovery_errors_follow_output_mode(self):
        self.helper.return_value = {'status': 'error', 'message': 'No account supports Recently Deleted on this Mac'}
        for command, argv in ((cli.cmd_deleted, ['deleted']),
                              (cli.cmd_info, ['info', '1', '--include-deleted']),
                              (cli.cmd_restore, ['restore', '1', '--list-id', '10', '--private'])):
            for json_mode in (False, True):
                with self.subTest(command=argv[0], json=json_mode):
                    code, payload, err = run(command, argv + (['--json'] if json_mode else []))
                    self.assertEqual(code, 1)
                    self.assertIsNone(payload)
                    if json_mode:
                        self.assertEqual(json.loads(err)['code'], 'recently_deleted_unavailable')
                    else:
                        self.assertEqual(err, 'Error: No account supports Recently Deleted on this Mac\n')

    def test_restore_preflight_errors_follow_output_mode_without_writes(self):
        cases = [(False, True, {}, 'private_required'),
                 (True, False, {}, 'private_helper_unavailable'),
                 (True, True, {'ok': True, 'version': 1}, 'private_helper_outdated'),
                 (True, True, {'ok': False, 'message': 'Protocol probe failed'}, 'private_helper_unavailable')]
        for enabled, available, probe, expected in cases:
            for json_mode in (False, True):
                with self.subTest(code=expected, json=json_mode), \
                        mock.patch.object(cli, 'require_private_metadata', side_effect=require_private_metadata), \
                        mock.patch.object(cli, 'private_metadata_enabled', return_value=enabled), \
                        mock.patch.object(cli, 'private_available', return_value=available), \
                        mock.patch.object(cli, '_probe_private_protocol_version', return_value=probe):
                    code, payload, err = run(cli.cmd_restore, ['restore', '1', '--list-id', '10'] + (['--json'] if json_mode else []))
                    self.assertEqual(code, 1)
                    self.assertIsNone(payload)
                    if json_mode:
                        self.assertEqual(json.loads(err)['code'], expected)
                    else:
                        self.assertTrue(err.startswith('Error: '))
                    self.helper.assert_not_called()

    def test_recovery_rejects_old_helper_before_querying_or_writing(self):
        for command, argv in ((cli.cmd_deleted, ['deleted', '--json']),
                              (cli.cmd_info, ['info', '1', '--include-deleted', '--json']),
                              (cli.cmd_restore, ['restore', '1', '--list-id', '10', '--private', '--json'])):
            with self.subTest(command=argv[0]), \
                    mock.patch.object(cli, 'require_private_metadata', side_effect=require_private_metadata), \
                    mock.patch.object(cli, '_probe_private_protocol_version', return_value={'ok': True, 'version': 2}):
                code, payload, err = run(command, argv)
                self.assertEqual(code, 1)
                self.assertIsNone(payload)
                self.assertEqual(json.loads(err)['code'], 'private_helper_outdated')
                self.assertIn('protocol 2 < required 3', json.loads(err)['message'])
                self.helper.assert_not_called()

    def test_restore_destination_errors_follow_output_mode(self):
        cases = [(None, 'list_not_found'), ({'error': 'ambiguous', 'candidates': [{'id': 10, 'title': 'Recovered'}]}, 'list_ambiguous'),
                 ({'error': 'group', 'group': {'title': 'Group', 'children': []}}, 'list_is_group')]
        for result, expected in cases:
            for json_mode in (False, True):
                with self.subTest(code=expected, json=json_mode), \
                        mock.patch.object(cli, 'resolve_required_list_target_or_die', side_effect=resolve_required_list_target), \
                        mock.patch.object(cli, 'resolve_list_ref', return_value=result):
                    code, payload, err = run(cli.cmd_restore, ['restore', '1', '--list', 'Missing', '--private'] + (['--json'] if json_mode else []))
                    self.assertEqual(code, 1)
                    self.assertIsNone(payload)
                    if json_mode:
                        self.assertEqual(json.loads(err)['code'], expected)
                    else:
                        self.assertTrue(err.startswith('Error: '))
                    self.helper.assert_not_called()

    def test_unknown_and_child_restore_do_not_write(self):
        for n, expected in [(4, 'reminder_not_recoverable'), (2, 'restore_parent_required')]:
            self.helper.reset_mock()
            code, _, err = run(cli.cmd_restore, ['restore', str(n), '--list-id', '10', '--private', '--json'])
            self.assertEqual(code, 1)
            self.assertIn(expected, err)
            self.assertEqual([c.args[0]['action'] for c in self.helper.call_args_list], ['recently_deleted'])

    def test_active_restore_is_noop_and_never_moves_to_another_list(self):
        self.db.execute('UPDATE ZREMCDREMINDER SET ZMARKEDFORDELETION=0,ZLIST=10 WHERE Z_PK=1')
        code, p, _ = run(cli.cmd_restore, ['restore', '1', '--list-id', '10', '--private', '--json'])
        self.assertEqual((code, p['status']), (0, 'already_restored'))
        # The child is still deleted: the no-op must not claim tree verification.
        self.assertNotIn('verified', p)
        self.helper.assert_not_called()
        self.target['id'] = 11
        code, _, err = run(cli.cmd_restore, ['restore', '1', '--list-id', '11', '--private', '--json'])
        self.assertEqual(code, 1)
        self.assertIn('reminder_not_deleted', err)
        self.helper.assert_not_called()

    def restore_with(self, update, response):
        def helper(request):
            if request['action'] == 'recently_deleted':
                return self.result
            update()
            return response
        self.helper.side_effect = helper
        return run(cli.cmd_restore, ['restore', '1', '--list-id', '10', '--private', '--json'])

    def test_restore_verifies_entire_tree_even_after_helper_error_without_retry(self):
        code, p, _ = self.restore_with(
            lambda: self.db.execute('UPDATE ZREMCDREMINDER SET ZMARKEDFORDELETION=0,ZLIST=10 WHERE Z_PK IN (1,2)'),
            {'status': 'error', 'message': 'connection closed'})
        self.assertEqual((code, p['status'], p['verified']), (0, 'restored', True))
        self.assertEqual(sum(c.args[0]['action'] == 'restore_reminder' for c in self.helper.call_args_list), 1)

    def test_partial_restore_or_wrong_identity_never_claims_success(self):
        for sql in ('UPDATE ZREMCDREMINDER SET ZMARKEDFORDELETION=0,ZLIST=10 WHERE Z_PK=1',
                    'UPDATE ZREMCDREMINDER SET ZMARKEDFORDELETION=0,ZLIST=10,ZPARENTREMINDER=NULL WHERE Z_PK IN (1,2)',
                    "UPDATE ZREMCDREMINDER SET ZMARKEDFORDELETION=0,ZLIST=10,ZCKIDENTIFIER='wrong' WHERE Z_PK IN (1,2)"):
            with self.subTest(sql=sql):
                self.db.execute('UPDATE ZREMCDREMINDER SET ZMARKEDFORDELETION=1,ZLIST=NULL')
                self.db.execute('UPDATE ZREMCDREMINDER SET ZPARENTREMINDER=1 WHERE Z_PK=2')
                self.db.executemany('UPDATE ZREMCDREMINDER SET ZCKIDENTIFIER=? WHERE Z_PK=?',[(identifier(n),n) for n in (1,2)])
                code, p, err = self.restore_with(lambda: self.db.execute(sql), {'status': 'updated'})
                self.assertEqual(code, 1)
                self.assertIsNone(p)
                self.assertIn('restore_unconfirmed', err)

    def test_mcp_tools_enforce_private_and_list_and_use_real_cli_parser(self):
        for name, args, command in [('recently_deleted', {}, 'deleted'),
                                    ('restore_reminder', {'reminder_id': 1, 'list': '-Work', 'private': True}, 'restore'),
                                    ('get_reminder', {'reminder_id': 1, 'include_deleted': True}, 'info')]:
            tool = mcp.TOOLS_BY_NAME[name]
            argv = tool.build_argv(mcp.validate_arguments(tool, args))
            parsed = cli.build_parser()[0].parse_args(argv)
            self.assertEqual(parsed.cmd, command)
        tool = mcp.TOOLS_BY_NAME['restore_reminder']
        for args in ({'reminder_id': 1, 'private': True}, {'reminder_id': 1, 'list': 'Work', 'private': False},
                     {'reminder_id': 1, 'list': 'Work', 'list_id': 2, 'private': True}):
            with self.assertRaises(mcp.ToolArgumentError):
                tool.build_argv(mcp.validate_arguments(tool, args))
        self.assertTrue(mcp.TOOLS_BY_NAME['recently_deleted'].read_only)
        self.assertFalse(tool.destructive)
        hints = mcp.result_ui_meta(mcp.TOOLS_BY_NAME['recently_deleted'], apps=True, legacy_aliases=False)
        self.assertNotIn('actions', hints[mcp.UI_RESULT_META_KEY])
