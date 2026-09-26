from __future__ import annotations

import contextlib
import io
import json
import socket
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import load_module
import remctl_runtime
from remctl_serialization import recurrence_from_row


class IssueCliFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_module('remctl_issue_cli', 'remctl')

    def test_image_options_reach_add_and_edit(self):
        for command in ('add', 'edit'):
            for option in (['--image', '/tmp/test.png'], ['--image=/tmp/test.png']):
                with self.subTest(command=command, option=option):
                    parser, sub = self.cli.build_parser()
                    args = self.cli.parse_cli_args(parser, sub, [command, 'test' if command == 'add' else '42', '--private', *option])
                    self.assertEqual(args.image, ['/tmp/test.png'])

    def test_exported_recurrence_import_and_subtask_keep_all_fields(self):
        row = dict(recurrence_frequency=2, recurrence_interval=2,
                   recurrence_days_of_week=json.dumps([{'dayOfTheWeek': 6, 'weekNumber': -1}]),
                   recurrence_count=8)
        exported = recurrence_from_row(row)
        expected = {'frequency': 'monthly', 'interval': 2, 'daysOfWeek': [6], 'weekNumbers': [-1], 'count': 8}
        self.assertEqual(self.cli.parse_recurrence(exported), expected)
        seen = []
        def add(args):
            seen.append(self.cli.recurrence_or_die(args.recurrence))
            print('{"numericId":42}')
        with (mock.patch('sys.stdin', io.StringIO(json.dumps([{'title':'Repeat', 'recurrence':exported}]))),
              mock.patch.object(self.cli, 'cmd_add', side_effect=add),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            self.cli.cmd_import(SimpleNamespace(file='-', json=True))
        self.assertEqual(seen, [expected])
        child = self.cli.parse_subtask_specs([json.dumps({'title':'Child', 'recurrence':exported})])[0]
        self.assertEqual(child['recurrence'], expected)
        for spec in (
            {'frequency':'yearly','interval':1,'monthsOfYear':[3,9],'daysOfMonth':[-1],'endDate':'2028-09-26T09:00:00'},
            {'frequency':'yearly','daysOfYear':[60,-1],'weeksOfYear':[1,-1],'setPositions':[1,-1]},
            {'frequency':'daily','endDate':'2028-09-26T09:00'},
            {'frequency':'daily','endDate':'2028-W39-2'},
            {'frequency':'daily','endDate':'2028-09-26T09:00:00.125'},
        ):
            normalized = self.cli.parse_recurrence(spec)
            self.assertIsNotNone(normalized)
            self.assertEqual(self.cli.parse_recurrence(normalized), normalized)

    def test_invalid_recurrence_preflights_entire_import(self):
        invalid = [42, [], {}, {'frequency':[]}, {'frequency':'monthly','daysOfWeek':[8]},
                   {'frequency':'monthly','daysOfWeek':[6],'weekNumbers':[6]},
                   {'frequency':'daily','count':True}, {'frequency':'daily','interval':0},
                   {'frequency':'daily','endDate':'bad'}, {'frequency':'daily','count':2,'endDate':'2027-01-01'},
                   {'frequency':'yearly','monthsOfYear':[13]}, {'frequency':'monthly','daysOfMonth':[0]},
                   {'frequency':'weekly','unknown':2}, {'frequency':'monthly','daysOfWeekDetailed':[None]}]
        for recurrence in invalid:
            with self.subTest(recurrence=recurrence):
                payload = [{'title':'Valid'}, {'title':'Invalid','recurrence':recurrence}]
                with (mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), mock.patch.object(self.cli,'cmd_add') as add,
                      contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err,
                      self.assertRaises(SystemExit)):
                    self.cli.cmd_import(SimpleNamespace(file='-', json=True))
                add.assert_not_called()
                self.assertEqual(json.loads(err.getvalue())['code'], 'invalid_import')

    def test_fake_ip_dns_allows_names_but_not_literals_or_mixed_private_answers(self):
        def addresses(*ips):
            return [(None,None,None,None,(ip,443)) for ip in ips]
        for ip in ('198.18.0.0','198.18.6.138','198.19.255.255'):
            with mock.patch('socket.getaddrinfo',return_value=addresses(ip)):
                self.assertTrue(remctl_runtime.is_safe_remote_url('https://example.com/'))
                self.assertFalse(remctl_runtime.is_safe_remote_url(f'https://{ip}/'))
        for url in ('https://0xc6120001/', 'https://3323068417/', 'https://198.18.1/', 'https://[::ffff:198.18.0.1]/'):
            with mock.patch('socket.getaddrinfo',return_value=addresses('198.18.0.1')):
                self.assertFalse(remctl_runtime.is_safe_remote_url(url))
        for ips in (('198.18.0.1','10.0.0.1'), ('198.17.255.255',), ('198.20.0.0',)):
            with mock.patch('socket.getaddrinfo',return_value=addresses(*ips)):
                self.assertEqual(remctl_runtime.is_safe_remote_url('https://example.com/'), len(ips)==1)
        with mock.patch('socket.getaddrinfo',side_effect=socket.gaierror):
            self.assertFalse(remctl_runtime.is_safe_remote_url('https://example.com/'))
        self.assertFalse(remctl_runtime.is_safe_remote_url('https://example.com:invalid/'))

    @unittest.skipUnless(shutil.which("swiftc"), "requires Swift")
    def test_eventkit_keeps_advanced_rule_fields(self):
        source = (Path(__file__).resolve().parents[1] / "remctl-bridge.swift").read_text()
        source = source.split("// MARK: - Color mapping")[0]
        source += r'''
let ruleJSON = #"{"frequency":"yearly","interval":2,"monthsOfYear":[3,9],"daysOfMonth":[-1],"setPositions":[1],"count":8}"#
let spec = try! JSONDecoder().decode(RecurrenceSpec.self, from: Data(ruleJSON.utf8))
let rule = buildRecurrenceRule(spec)!
assert(rule.interval == 2)
assert(rule.monthsOfTheYear == [3,9])
assert(rule.daysOfTheMonth == [-1])
assert(rule.setPositions == [1])
assert(rule.recurrenceEnd?.occurrenceCount == 8)
let endJSON = #"{"frequency":"daily","end":"2028-09-26T09:00:00"}"#
let endSpec = try! JSONDecoder().decode(RecurrenceSpec.self, from: Data(endJSON.utf8))
assert(buildRecurrenceRule(endSpec)!.recurrenceEnd!.endDate == parseISO("2028-09-26T09:00:00"))
'''
        for end in ('2028-09-26T09:00', '2028-W39-2', '2028-09-26T09:00:00.125', '2028-09-26T09:00:00.125+02:00'):
            normalized = self.cli.parse_recurrence({'frequency':'daily','endDate':end})
            literal = json.dumps(normalized)
            source += f'\nassert(buildRecurrenceRule(try! JSONDecoder().decode(RecurrenceSpec.self, from: Data(#"{literal}"#.utf8))) != nil)\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.swift"
            binary = Path(tmp) / "rules"
            path.write_text(source)
            subprocess.run(["swiftc", str(path), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)
