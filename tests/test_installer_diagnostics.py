from __future__ import annotations

import ast
import contextlib
import io
import os
import plistlib
import subprocess
import tempfile
import remctl_broker
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
        status = {'app':{'installed':True,'bundleIdentifier':remctl_broker.BUNDLE_IDENTIFIER,'signature':{'valid':True,'identifier':remctl_broker.BUNDLE_IDENTIFIER}},
                  'launchAgent':{'installed':True,'loaded':False,'contractValid':True,'path':path}}
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

    def test_bootstrap_guidance_rejects_foreign_or_malformed_plists(self):
        cli = load_module('remctl_plist_diagnostics', 'remctl')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'agent.plist'
            app = Path(tmp) / 'RemCTL Capability Host.app'
            executable = app / 'Contents/MacOS/RemCTL Capability Host'
            sock = Path(tmp) / 'capability-host.sock'
            template = plistlib.loads((ROOT / 'remctl-capability-host-launchagent.plist').read_bytes())
            template['ProgramArguments'] = [str(executable),'--run-capability-host','--socket',str(sock)]
            for variant in (template, {**template, 'Label':'foreign'}, {**template, 'Program':'/bin/sh'},
                            {**template,'EnvironmentVariables':{'DYLD_INSERT_LIBRARIES':'/tmp/library'}},
                            {**template,'RunAtLoad':1}, {**template,'KeepAlive':1}, {**template,'Umask':63.0}, 'broken'):
                with self.subTest(variant=variant):
                    path.write_bytes(plistlib.dumps(variant) if isinstance(variant,dict) else b'not a plist')
                    with (mock.patch.object(remctl_broker,'launch_agent_path',return_value=path),
                          mock.patch.object(remctl_broker,'executable_path',return_value=executable),
                          mock.patch.object(remctl_broker,'socket_path',return_value=sock),
                          mock.patch.object(remctl_broker.subprocess,'run',return_value=subprocess.CompletedProcess([],1))):
                        status = remctl_broker._launch_agent_status()
                    self.assertEqual(status['contractValid'],variant is template)
                    if variant is not template:
                        self.assertIsNone(cli.capability_host_start_fix({'app':{'installed':True,'signature':{'valid':True}},'launchAgent':status}))

    def test_direct_mode_does_not_recommend_starting_the_bypassed_host(self):
        cli = load_module('remctl_direct_diagnostics', 'remctl')
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / 'missing'
            with (mock.patch.object(cli,'capability_host_requested_mode',return_value='direct'),
                  mock.patch.object(cli,'capability_host_start_fix',return_value='START THE HOST') as start_fix,
                  mock.patch.object(cli,'reminders_store_access_error',return_value='blocked'),
                  mock.patch.object(cli,'find_main_db_path',return_value=None),
                  mock.patch.object(cli,'current_bridge_path',return_value=absent),
                  mock.patch.object(cli,'current_private_path',return_value=absent)):
                checks = cli.gather_doctor_checks({'installed':True,'ready':False})
            start_fix.assert_not_called()
            effective = next(check for check in checks if check['name']=='effective_access')
            self.assertIn('route=direct',effective['detail'])
            self.assertNotIn('START THE HOST',effective['fix'])

    def test_installer_and_uninstaller_require_boolean_and_integer_plist_types(self):
        scripts = []
        for filename, function, terminator in (('install.sh','installed_agent_owned()', 'PY'), ('uninstall.sh','agent_owned()', 'PYTHON')):
            section = (ROOT / filename).read_text().split(function,1)[1]
            code = section.split("<<'"+terminator+"'\n",1)[1].split('\n'+terminator+'\n',1)[0]
            scripts.append(code)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'agent.plist'
            expected = plistlib.loads((ROOT / 'remctl-capability-host-launchagent.plist').read_bytes())
            expected['ProgramArguments'] = ['/host','--run-capability-host','--socket','/socket']
            for value, code in ((value,code) for value in (expected, {**expected,'RunAtLoad':1}, {**expected,'KeepAlive':1}, {**expected,'Umask':63.0}) for code in scripts):
                path.write_bytes(plistlib.dumps(value))
                result = subprocess.run([os.sys.executable,'-I','-S','-',str(path),expected['Label'],'/host','/socket'], input=code, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0 if value is expected else 1, result.stderr)
