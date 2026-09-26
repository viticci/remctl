from __future__ import annotations

import ast
import contextlib
import io
import os
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from helpers import load_module

ROOT = Path(__file__).resolve().parents[1]


class InstallerDiagnosticTests(unittest.TestCase):
    def test_protected_python_reports_exact_failed_path_and_condition(self):
        source = (ROOT / 'install.sh').read_text().split('# Validate the interpreter')[1]
        source = source.split("<<'PY'\n", 1)[1].split('\nPY\n',1)[0]
        tree = ast.parse(source)
        tree.body = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))]
        namespace = {'wheel_safe': True}
        exec(compile(tree, 'installer_validator', 'exec'), namespace)
        cases = [(0,80,0o775,'writable by a non-root group'), (501,20,0o755,'not owned by root'),
                 (0,0,0o777,'writable by everyone'), (0,0,0o755,'extended ACL')]
        for uid,gid,mode,reason in cases:
            with self.subTest(reason=reason):
                metadata = SimpleNamespace(st_uid=uid,st_gid=gid,st_mode=stat.S_IFREG|mode)
                namespace['no_acl'] = lambda _: reason != 'extended ACL'
                with mock.patch('os.stat',return_value=metadata), contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertFalse(namespace['protected']('/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13'))
                self.assertIn(reason,err.getvalue())
                self.assertIn('uid='+str(uid),err.getvalue())
                self.assertIn(f'mode={mode:04o}',err.getvalue())
                self.assertIn('/Versions/3.13/bin/python3.13',err.getvalue())
        namespace['no_acl'] = lambda _: True
        with mock.patch('os.stat',return_value=SimpleNamespace(st_uid=0,st_gid=0,st_mode=stat.S_IFDIR|0o775)):
            self.assertTrue(namespace['protected']('/Library'))

    def test_doctor_gives_start_command_only_for_valid_unloaded_host(self):
        cli = load_module('remctl_installer_diagnostics', 'remctl')
        path = str(Path.home() / '.local/Library/LaunchAgents/net.macstories.remctl.capability-host.plist')
        status = {'app':{'installed':True,'signature':{'valid':True}},
                  'launchAgent':{'installed':True,'loaded':False,'path':path}}
        fix = cli.capability_host_start_fix(status)
        self.assertIn(f'launchctl bootstrap gui/{os.getuid()}',fix)
        self.assertIn(path,fix)
        self.assertIn('not loaded at login',fix)
        self.assertNotIn('onboard',fix)
        status['app']['signature']['valid'] = False
        self.assertIsNone(cli.capability_host_start_fix(status))
        status['app']['signature']['valid'] = True
        status['launchAgent']['loaded'] = True
        self.assertIsNone(cli.capability_host_start_fix(status))
