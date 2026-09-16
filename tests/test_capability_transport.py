import array
import base64
import contextlib
import io
import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import remctl_broker
import remctl_capabilities
from remctl_capabilities import (
    CAPABILITY_PREFIX,
    CapabilityBundle,
    CapabilityError,
    DescriptorCapability,
    materialize_inputs,
    plan_invocation,
    validate_capability_bindings,
    validate_received_capabilities,
)


class _NonTTYOpenPipe:
    def isatty(self):
        return False

    def read(self, *_args, **_kwargs):
        raise AssertionError("the planner must not read non-TTY stdin")


class _NonTTY:
    def isatty(self):
        return False


def _capability(identifier="input-0", purpose="image"):
    return DescriptorCapability(
        identifier=identifier,
        fd=-1,
        kind="input-file",
        purpose=purpose,
        device=1,
        inode=2,
        mode=0o100600,
        size=3,
        name="image.png",
    )


class CapabilityBindingTests(unittest.TestCase):
    def test_tty_access_mode_must_match_purpose(self):
        master_fd, slave_fd = pty.openpty()
        tty_path = os.ttyname(slave_fd)

        def metadata(fd, purpose):
            details = os.fstat(fd)
            return [
                {
                    "id": "tty-0",
                    "fdIndex": 0,
                    "kind": "tty",
                    "purpose": purpose,
                    "device": details.st_dev,
                    "inode": details.st_ino,
                    "mode": details.st_mode,
                    "size": 0,
                    "name": None,
                    "columns": None,
                }
            ]

        write_only = os.open(tty_path, os.O_WRONLY | getattr(os, "O_NOCTTY", 0))
        read_only = os.open(
            tty_path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0),
        )
        try:
            with self.assertRaisesRegex(CapabilityError, "access mode"):
                validate_received_capabilities(metadata(write_only, "stdin"), [write_only])
            for purpose in ("stdout", "stderr"):
                with self.subTest(purpose=purpose), self.assertRaisesRegex(
                    CapabilityError,
                    "access mode",
                ):
                    validate_received_capabilities(metadata(read_only, purpose), [read_only])
        finally:
            os.close(read_only)
            os.close(write_only)
            os.close(slave_fd)
            os.close(master_fd)

    def test_received_input_descriptor_must_be_read_only(self):
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "input.json"
            source.write_bytes(b"{}")
            fd = os.open(source, os.O_RDWR)
            try:
                details = os.fstat(fd)
                metadata = [
                    {
                        "id": "input-0",
                        "fdIndex": 0,
                        "kind": "input-file",
                        "purpose": "import",
                        "device": details.st_dev,
                        "inode": details.st_ino,
                        "mode": details.st_mode,
                        "size": details.st_size,
                        "name": "input.json",
                        "columns": None,
                    }
                ]
                with self.assertRaisesRegex(CapabilityError, "read-only"):
                    validate_received_capabilities(metadata, [fd])
            finally:
                os.close(fd)

    def test_rejects_raw_file_fields_for_every_supported_surface(self):
        cases = [
            (["import", "/protected/data.json"], SimpleNamespace(cmd="import", file="/protected/data.json")),
            (["add", "Title", "--image", "/protected/a.png"], SimpleNamespace(cmd="add", image=["/protected/a.png"], subtask=None)),
            (
                ["add", "Title", "--subtask", '{"title":"Child","image":"/protected/a.png"}'],
                SimpleNamespace(
                    cmd="add",
                    image=None,
                    subtask=['{"title":"Child","image":"/protected/a.png"}'],
                ),
            ),
            (
                ["smart-list-create", "Flagged", "--filter-json", "@/protected/filter.json"],
                SimpleNamespace(cmd="smart-list-create", filter_json="@/protected/filter.json"),
            ),
        ]
        for argv, parsed in cases:
            with self.subTest(argv=argv), self.assertRaises(CapabilityError):
                validate_capability_bindings(argv, parsed, {})

    def test_requires_exact_id_purpose_and_single_occurrence(self):
        marker = CAPABILITY_PREFIX + "input-0"
        parsed = SimpleNamespace(cmd="add", image=[marker], subtask=None)
        validate_capability_bindings(
            ["add", "Title", "--image", marker],
            parsed,
            {"input-0": _capability()},
        )
        with self.assertRaisesRegex(CapabilityError, "purpose"):
            validate_capability_bindings(
                ["add", "Title", "--image", marker],
                parsed,
                {"input-0": _capability(purpose="import")},
            )
        with self.assertRaisesRegex(CapabilityError, "more than once"):
            validate_capability_bindings(
                ["add", "Title", "--image", marker, "--image", marker],
                SimpleNamespace(cmd="add", image=[marker, marker], subtask=None),
                {"input-0": _capability()},
            )
        with self.assertRaisesRegex(CapabilityError, "exactly match"):
            validate_capability_bindings(
                ["add", "Title"],
                SimpleNamespace(cmd="add", image=None, subtask=None),
                {"input-0": _capability()},
            )

    def test_rejects_capability_marker_in_non_file_argument(self):
        marker = CAPABILITY_PREFIX + "input-0"
        with self.assertRaisesRegex(CapabilityError, "occurrence"):
            validate_capability_bindings(
                ["add", marker, "--image", marker],
                SimpleNamespace(cmd="add", image=[marker], subtask=None),
                {"input-0": _capability()},
            )

    def test_materialization_replaces_only_exact_file_fields(self):
        marker = CAPABILITY_PREFIX + "input-0"
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / "photo.png"
            source.write_bytes(b"png")
            fd = os.open(source, os.O_RDONLY)
            try:
                details = os.fstat(fd)
                item = DescriptorCapability(
                    identifier="input-0",
                    fd=fd,
                    kind="input-file",
                    purpose="image",
                    device=details.st_dev,
                    inode=details.st_ino,
                    mode=details.st_mode,
                    size=details.st_size,
                    name="photo.png",
                )
                title = f"literal-{marker}-text"
                rewritten, stdin_fd, columns, stderr_fd = materialize_inputs(
                    ["add", title, "--image", marker],
                    {"input-0": item},
                    Path(temp_value) / "stage",
                )
            finally:
                os.close(fd)
        self.assertEqual(rewritten[1], title)
        self.assertTrue(rewritten[3].endswith("-photo.png"))
        self.assertIsNone(stdin_fd)
        self.assertIsNone(columns)
        self.assertIsNone(stderr_fd)


class CapabilityPlannerTests(unittest.TestCase):
    def test_tty_duplicate_is_closed_when_descriptor_inspection_fails(self):
        master, original = pty.openpty()
        stream = SimpleNamespace(isatty=lambda: True, fileno=lambda: original)
        try:
            for target in ("os.fstat", "fcntl.fcntl"):
                with self.subTest(target=target):
                    duplicate = os.dup(original)
                    try:
                        with (
                            mock.patch.object(remctl_capabilities.os, "dup", return_value=duplicate),
                            mock.patch("remctl_capabilities." + target, side_effect=OSError("inspection failed")),
                        ):
                            result = remctl_capabilities._open_tty(
                                stream, identifier="tty-0", purpose="stdin"
                            )
                        self.assertIsNone(result)
                        with self.assertRaises(OSError):
                            os.fstat(duplicate)
                        os.fstat(original)
                    finally:
                        try:
                            os.close(duplicate)
                        except OSError:
                            pass
        finally:
            os.close(original)
            os.close(master)

    def test_installed_markers_resolve_custom_app_and_socket(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value).resolve()
            client = root / "bin"
            app = root / "Applications" / "RemCTL Capability Host.app"
            resources = app / "Contents" / "Resources"
            socket_path = root / "state" / "capability.sock"
            agent_path = (
                root
                / "Custom Agents"
                / "net.macstories.remctl.capability-host.plist"
            )
            client.mkdir()
            resources.mkdir(parents=True)
            (client / ".remctl-capability-host-app").write_text(str(app) + "\n")
            (resources / "remctl-capability-host-socket-path").write_text(
                str(socket_path) + "\n"
            )
            (resources / "remctl-capability-host-launch-agent-path").write_text(
                str(agent_path) + "\n"
            )
            with (
                mock.patch.object(remctl_broker, "CLIENT_ROOT", client),
                mock.patch.dict(
                    os.environ,
                    {
                        "REMCTL_CAPABILITY_HOST_APP": "",
                        "REMCTL_CAPABILITY_HOST_SOCKET": "",
                    },
                ),
            ):
                self.assertEqual(remctl_broker.app_path(), app)
                self.assertEqual(remctl_broker.socket_path(), socket_path)
                self.assertEqual(remctl_broker.launch_agent_path(), agent_path)

    def test_real_parser_planner_and_server_binding_cover_all_file_surfaces(self):
        parser = remctl_broker._load_real_parser()
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            data = root / "reminders.json"
            image = root / "cover art.png"
            nested = root / "nested.jpg"
            filter_file = root / "filter.json"
            data.write_text("[]")
            image.write_bytes(b"image")
            nested.write_bytes(b"nested")
            filter_file.write_text("{}")
            cases = [
                ["import", str(data)],
                ["add", "Title", "--image=" + str(image)],
                [
                    "edit",
                    "123",
                    "--subtask",
                    json.dumps({"title": "Child", "images": [str(nested)]}),
                ],
                [
                    "smart-list-create",
                    "Flagged",
                    "--filter-json",
                    "@" + str(filter_file),
                ],
            ]
            for argv in cases:
                with self.subTest(argv=argv):
                    parsed = parser.parse_args(argv)
                    with plan_invocation(
                        argv,
                        parsed,
                        stdin=_NonTTY(),
                        stdout=_NonTTY(),
                        stderr=_NonTTY(),
                    ) as bundle:
                        _validated, hosted = remctl_broker.validate_argv(
                            bundle.argv,
                            parser=parser,
                        )
                        received = validate_received_capabilities(
                            bundle.metadata,
                            bundle.fds,
                        )
                        validate_capability_bindings(bundle.argv, hosted, received)

    def test_non_tty_open_pipe_is_never_read(self):
        parsed = SimpleNamespace(cmd="today")
        with plan_invocation(
            ["today"],
            parsed,
            stdin=_NonTTYOpenPipe(),
            stdout=_NonTTY(),
            stderr=_NonTTY(),
        ) as bundle:
            self.assertEqual(bundle.stdin_bytes, b"")
            self.assertEqual(bundle.descriptors, [])

    def test_dispatch_wraps_local_planning_rejection(self):
        with mock.patch.object(
            remctl_broker,
            "plan_invocation",
            side_effect=CapabilityError("unsafe path"),
        ):
            with self.assertRaises(remctl_broker.HostRejected) as raised:
                remctl_broker.dispatch(["today"], parsed_args=SimpleNamespace(cmd="today"))
        self.assertEqual(raised.exception.code, "invalid_local_capability_request")
        self.assertTrue(raised.exception.retry_safe)

    def test_run_send_failure_after_send_attempt_is_indeterminate(self):
        class FakeConnection:
            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def close(self):
                pass

        partial = []

        def fail_after_partial_send(*_args, **_kwargs):
            partial.append(b"{")
            raise OSError("connection reset after one byte")

        with (
            mock.patch.object(remctl_broker, "_socket_metadata", return_value={"secure": True}),
            mock.patch.object(remctl_broker.socket, "socket", return_value=FakeConnection()),
            mock.patch.object(remctl_broker, "_send_frame", side_effect=fail_after_partial_send),
        ):
            with self.assertRaises(remctl_broker.HostIndeterminate) as raised:
                remctl_broker._request(
                    {"protocolVersion": remctl_broker.PROTOCOL_VERSION, "operation": "run"},
                    timeout=1,
                )
        self.assertEqual(partial, [b"{"])
        self.assertTrue(raised.exception.dispatched)
        self.assertTrue(raised.exception.indeterminate)

    def test_run_signals_during_send_are_deferred_until_the_frame_is_complete(self):
        class FakeConnection:
            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def close(self):
                pass

        for signal_number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signal_number):
                events = []
                forwarded = []

                def send_request(*_args, **_kwargs):
                    events.append("request-start")
                    signal.getsignal(signal_number)(signal_number, None)
                    events.append("request-complete")

                def send_control(_connection, actual_signal):
                    events.append(("control", actual_signal))
                    # A repeated signal in the post-frame forwarding window must
                    # not interrupt or create a second control frame.
                    signal.getsignal(signal_number)(signal_number, None)

                with (
                    mock.patch.object(remctl_broker, "_socket_metadata", return_value={"secure": True}),
                    mock.patch.object(remctl_broker.socket, "socket", return_value=FakeConnection()),
                    mock.patch.object(remctl_broker, "_send_frame", side_effect=send_request),
                    mock.patch.object(remctl_broker, "_send_run_signal_control", side_effect=send_control),
                    mock.patch.object(
                        remctl_broker,
                        "_recv_response",
                        return_value={"status": "completed"},
                    ),
                ):
                    response = remctl_broker._request(
                        {"protocolVersion": remctl_broker.PROTOCOL_VERSION, "operation": "run"},
                        timeout=1,
                        forwarded_signals=forwarded,
                    )

                self.assertEqual(response, {"status": "completed"})
                self.assertEqual(forwarded, [signal_number])
                self.assertEqual(
                    events,
                    ["request-start", "request-complete", ("control", signal_number)],
                )

    def test_run_signal_handlers_are_active_after_send_and_always_restored(self):
        class FakeConnection:
            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def close(self):
                pass

        for signal_number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signal_number):
                previous = signal.getsignal(signal_number)
                forwarded = []
                controls = []
                receives = 0

                def receive_after_send(*_args, **_kwargs):
                    nonlocal receives
                    receives += 1
                    if receives == 1:
                        signal.getsignal(signal_number)(signal_number, None)
                    return {"status": "completed"}

                with (
                    mock.patch.object(remctl_broker, "_socket_metadata", return_value={"secure": True}),
                    mock.patch.object(remctl_broker.socket, "socket", return_value=FakeConnection()),
                    mock.patch.object(remctl_broker, "_send_frame"),
                    mock.patch.object(
                        remctl_broker,
                        "_send_run_signal_control",
                        side_effect=lambda _connection, actual: controls.append(actual),
                    ),
                    mock.patch.object(remctl_broker, "_recv_response", side_effect=receive_after_send),
                ):
                    response = remctl_broker._request(
                        {"protocolVersion": remctl_broker.PROTOCOL_VERSION, "operation": "run"},
                        timeout=1,
                        forwarded_signals=forwarded,
                    )

                self.assertEqual(response, {"status": "completed"})
                self.assertEqual(forwarded, [signal_number])
                self.assertEqual(controls, [signal_number])
                self.assertIs(signal.getsignal(signal_number), previous)

    def test_run_signal_handlers_are_restored_when_partial_send_fails(self):
        class FakeConnection:
            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def close(self):
                pass

        previous = {
            signal_number: signal.getsignal(signal_number)
            for signal_number in (signal.SIGINT, signal.SIGTERM)
        }

        def interrupted_partial_send(*_args, **_kwargs):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            raise OSError("connection reset after partial request")

        forwarded = []
        with (
            mock.patch.object(remctl_broker, "_socket_metadata", return_value={"secure": True}),
            mock.patch.object(remctl_broker.socket, "socket", return_value=FakeConnection()),
            mock.patch.object(remctl_broker, "_send_frame", side_effect=interrupted_partial_send),
        ):
            with self.assertRaises(remctl_broker.HostIndeterminate):
                remctl_broker._request(
                    {"protocolVersion": remctl_broker.PROTOCOL_VERSION, "operation": "run"},
                    timeout=1,
                    forwarded_signals=forwarded,
                )

        self.assertEqual(forwarded, [signal.SIGINT])
        for signal_number, handler in previous.items():
            self.assertIs(signal.getsignal(signal_number), handler)

    def test_local_command_ignores_invalid_host_mode(self):
        with mock.patch.dict(os.environ, {"REMCTL_CAPABILITY_HOST": "invalid"}):
            self.assertFalse(remctl_broker.should_route(SimpleNamespace(cmd="doctor")))


class RunResponseSchemaTests(unittest.TestCase):
    def _dispatch_response(self, response):
        with (
            mock.patch.object(
                remctl_broker,
                "plan_invocation",
                return_value=CapabilityBundle(["today"]),
            ),
            mock.patch.object(remctl_broker, "_request", return_value=response),
        ):
            return remctl_broker.dispatch(
                ["today"],
                parsed_args=SimpleNamespace(cmd="today"),
            )

    def test_stale_completed_protocol_is_indeterminate(self):
        response = {
            "status": "completed",
            "protocolVersion": 999,
            "dispatched": True,
            "indeterminate": False,
            "exitCode": 0,
            "stdoutBase64": "",
            "stderrBase64": "",
            "stderrRelayed": False,
        }
        with self.assertRaises(remctl_broker.HostIndeterminate):
            self._dispatch_response(response)

    def test_indeterminate_error_replays_strict_bounded_partial_output(self):
        response = {
            "status": "error",
            "protocolVersion": remctl_broker.PROTOCOL_VERSION,
            "code": "command_timeout",
            "message": "timed out",
            "dispatched": True,
            "indeterminate": True,
            "stdoutBase64": base64.b64encode(b"saved stdout\n").decode(),
            "stderrBase64": base64.b64encode(b"saved stderr\n").decode(),
            "stderrRelayed": False,
        }
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        with (
            mock.patch.object(remctl_broker.sys, "stdout", stdout),
            mock.patch.object(remctl_broker.sys, "stderr", stderr),
            self.assertRaises(remctl_broker.HostIndeterminate),
        ):
            remctl_broker._handle_run_response(response)
        self.assertEqual(stdout.getvalue(), b"saved stdout\n")
        self.assertEqual(stderr.getvalue(), b"saved stderr\n")

        malformed = dict(response, stdoutBase64="not base64")
        with self.assertRaises(remctl_broker.HostIndeterminate) as raised:
            remctl_broker._handle_run_response(malformed)
        self.assertEqual(raised.exception.code, "malformed_run_response")

    def test_only_exact_explicit_predispatch_error_is_retry_safe(self):
        response = {
            "status": "error",
            "protocolVersion": remctl_broker.PROTOCOL_VERSION,
            "code": "invalid_request",
            "message": "rejected before dispatch",
            "dispatched": False,
            "indeterminate": False,
        }
        with self.assertRaises(remctl_broker.HostRejected) as raised:
            self._dispatch_response(response)
        self.assertTrue(raised.exception.retry_safe)

    def test_malformed_run_errors_are_indeterminate(self):
        base = {
            "status": "error",
            "protocolVersion": remctl_broker.PROTOCOL_VERSION,
            "code": "invalid_request",
            "message": "rejected",
            "dispatched": False,
            "indeterminate": False,
        }
        malformed = []
        for missing in ("dispatched", "indeterminate"):
            value = dict(base)
            del value[missing]
            malformed.append(value)
        value = dict(base)
        value["dispatched"] = 0
        malformed.append(value)
        value = dict(base)
        value["extra"] = True
        malformed.append(value)
        for response in malformed:
            with self.subTest(response=response), self.assertRaises(
                remctl_broker.HostIndeterminate
            ):
                self._dispatch_response(response)

    def test_completion_accepts_normal_and_terminating_signal_statuses(self):
        for exit_code in (0, 1, 255, -int(signal.SIGTERM), -int(signal.SIGKILL)):
            response = {
                "status": "completed",
                "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                "dispatched": True,
                "indeterminate": False,
                "exitCode": exit_code,
                "stdoutBase64": "",
                "stderrBase64": "",
                "stderrRelayed": False,
            }
            with self.subTest(exit_code=exit_code):
                self.assertEqual(self._dispatch_response(response), exit_code)

    def test_impossible_completion_statuses_are_indeterminate(self):
        for exit_code in (256, 1000, -999, True, 1.0):
            response = {
                "status": "completed",
                "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                "dispatched": True,
                "indeterminate": False,
                "exitCode": exit_code,
                "stdoutBase64": base64.b64encode(b"must not relay").decode("ascii"),
                "stderrBase64": "",
                "stderrRelayed": False,
            }
            with self.subTest(exit_code=exit_code), self.assertRaises(
                remctl_broker.HostIndeterminate
            ):
                self._dispatch_response(response)

    def test_output_over_protocol_bounds_is_indeterminate(self):
        response = {
            "status": "completed",
            "protocolVersion": remctl_broker.PROTOCOL_VERSION,
            "dispatched": True,
            "indeterminate": False,
            "exitCode": 0,
            "stdoutBase64": base64.b64encode(b"1234").decode("ascii"),
            "stderrBase64": "",
            "stderrRelayed": False,
        }
        with (
            mock.patch.object(remctl_broker, "MAX_STDOUT_BYTES", 3),
            self.assertRaises(remctl_broker.HostIndeterminate),
        ):
            self._dispatch_response(response)

    @unittest.skipUnless(os.name == "posix", "POSIX signal return codes are required")
    def test_client_reproduces_hosted_sigterm_after_relaying_output(self):
        response = {
            "status": "completed",
            "protocolVersion": remctl_broker.PROTOCOL_VERSION,
            "dispatched": True,
            "indeterminate": False,
            "exitCode": -int(signal.SIGTERM),
            "stdoutBase64": base64.b64encode(b"hosted stdout\n").decode("ascii"),
            "stderrBase64": base64.b64encode(b"hosted stderr\n").decode("ascii"),
            "stderrRelayed": False,
        }
        script = (
            "from types import SimpleNamespace\n"
            "from unittest import mock\n"
            "import remctl_broker\n"
            "from remctl_capabilities import CapabilityBundle\n"
            f"response = {response!r}\n"
            "with mock.patch.object(remctl_broker, 'plan_invocation', "
            "return_value=CapabilityBundle(['today'])), "
            "mock.patch.object(remctl_broker, '_request', return_value=response):\n"
            "    status = remctl_broker.dispatch(['today'], parsed_args=SimpleNamespace(cmd='today'))\n"
            "    remctl_broker.exit_with_status(status)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        self.assertEqual(completed.returncode, -int(signal.SIGTERM))
        self.assertEqual(completed.stdout, b"hosted stdout\n")
        self.assertEqual(completed.stderr, b"hosted stderr\n")


class MergedOutputTests(unittest.TestCase):
    def _completion(self, stdout=b"", stderr=b""):
        return {
            "status": "completed",
            "protocolVersion": remctl_broker.PROTOCOL_VERSION,
            "dispatched": True,
            "indeterminate": False,
            "exitCode": 0,
            "stdoutBase64": base64.b64encode(stdout).decode("ascii"),
            "stderrBase64": base64.b64encode(stderr).decode("ascii"),
            "stderrRelayed": False,
        }

    def _tty_capability(self, purpose):
        fd = os.dup(1)
        details = os.fstat(fd)
        return DescriptorCapability(
            identifier=f"tty-{purpose}",
            fd=fd,
            kind="tty",
            purpose=purpose,
            device=details.st_dev,
            inode=details.st_ino,
            mode=details.st_mode,
            size=0,
            name=None,
            columns=80,
        )

    def _runtime(self, root):
        return remctl_broker.HostedRuntime(
            app=root / "Host.app",
            runtime=root,
            archive_fd=198,
            cdhash="0" * 40,
            python=Path(sys.executable),
            bridge=root / "bridge",
            private=root / "private",
        )

    def test_dispatch_merges_stdout_tty_but_not_prompt_ttys(self):
        cases = (("stdout", True), ("stdin", False), ("stderr", False))
        for purpose, expected in cases:
            with self.subTest(purpose=purpose):
                bundle = CapabilityBundle(
                    ["today"],
                    descriptors=[self._tty_capability(purpose)],
                )
                with (
                    mock.patch.object(
                        remctl_broker,
                        "plan_invocation",
                        return_value=bundle,
                    ),
                    mock.patch.object(
                        remctl_broker,
                        "_output_streams_share_sink",
                        return_value=True,
                    ),
                    mock.patch.object(
                        remctl_broker,
                        "_request",
                        return_value=self._completion(),
                    ) as request,
                ):
                    remctl_broker.dispatch(
                        ["today"], parsed_args=SimpleNamespace(cmd="today")
                    )
                self.assertIs(request.call_args.args[0]["mergeOutput"], expected)

    def test_kernel_merged_pipe_preserves_alternating_flushed_output(self):
        code = (
            "import sys,time; "
            "sys.stdout.write('A');sys.stdout.flush();time.sleep(.03); "
            "sys.stderr.write('B');sys.stderr.flush();time.sleep(.03); "
            "sys.stdout.write('C');sys.stdout.flush();time.sleep(.03); "
            "sys.stderr.write('D');sys.stderr.flush()"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        exit_code, stdout, stderr, relayed = remctl_broker._collect_bounded_output(
            process,
            stdin_bytes=b"",
            stderr_relay_fd=None,
            timeout=3,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, b"ABCD")
        self.assertEqual(stderr, b"")
        self.assertFalse(relayed)

    def test_kernel_merged_pipe_preserves_stderr_first_when_both_are_ready(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os; os.write(2,b'B'); os.write(1,b'A')",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        exit_code, stdout, stderr, _relayed = remctl_broker._collect_bounded_output(
            process,
            stdin_bytes=b"",
            stderr_relay_fd=None,
            timeout=3,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, b"BA")
        self.assertEqual(stderr, b"")

    def test_host_spawn_merges_once_without_duplicate_response_bytes(self):
        class Process:
            pid = 424242
            returncode = 0

            def poll(self):
                return 0

        with tempfile.TemporaryDirectory() as temp_value:
            with (
                mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
                mock.patch.object(remctl_broker, "_load_real_parser", return_value=object()),
                mock.patch.object(
                    remctl_broker,
                    "validate_argv",
                    return_value=(["today"], object()),
                ),
                mock.patch.object(
                    remctl_broker,
                    "materialize_inputs",
                    return_value=(["today"], None, None, None),
                ),
                mock.patch.object(
                    remctl_broker,
                    "_collect_bounded_output",
                    return_value=(0, b"BA", b"", False),
                ),
                mock.patch.object(
                    remctl_broker.subprocess,
                    "Popen",
                    return_value=Process(),
                ) as popen,
            ):
                response = remctl_broker._run_cli(
                    ["today"],
                    runtime=self._runtime(Path(temp_value)),
                    capabilities={},
                    stdin_bytes=b"",
                    timeout=1,
                    merge_output=True,
                )
        self.assertIs(popen.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(base64.b64decode(response["stdoutBase64"]), b"BA")
        self.assertEqual(response["stderrBase64"], "")
        self.assertNotIn("streamEvents", response)

    def test_separate_sinks_retain_their_exact_bytes(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            remctl_broker._handle_run_response(self._completion(b"AC", b"BD"))
        self.assertEqual(stdout.getvalue(), "AC")
        self.assertEqual(stderr.getvalue(), "BD")

    @unittest.skipUnless(hasattr(os, "openpty"), "PTY support is required")
    def test_same_pty_stdout_capability_requests_merge_and_replays_once(self):
        with tempfile.TemporaryDirectory() as temp_value:
            merge_marker = Path(temp_value) / "merge.json"
            script = (
                "import json,os,sys\n"
                "from pathlib import Path\n"
                "from types import SimpleNamespace\n"
                "from unittest import mock\n"
                "import remctl_broker\n"
                "from remctl_capabilities import CapabilityBundle,DescriptorCapability\n"
                "fd=os.dup(1); d=os.fstat(fd)\n"
                "cap=DescriptorCapability(identifier='tty-stdout',fd=fd,kind='tty',"
                "purpose='stdout',device=d.st_dev,inode=d.st_ino,mode=d.st_mode,size=0,"
                "name=None,columns=80)\n"
                f"marker=Path({str(merge_marker)!r})\n"
                "response={'status':'completed','protocolVersion':remctl_broker.PROTOCOL_VERSION,"
                "'dispatched':True,'indeterminate':False,'exitCode':0,"
                "'stdoutBase64':'QkE=','stderrBase64':'','stderrRelayed':False}\n"
                "def request(payload,**kwargs):\n"
                "    marker.write_text(json.dumps({'mergeOutput':payload['mergeOutput']}))\n"
                "    return response\n"
                "with mock.patch.object(remctl_broker,'plan_invocation',"
                "return_value=CapabilityBundle(['today'],descriptors=[cap])), "
                "mock.patch.object(remctl_broker,'_request',side_effect=request):\n"
                "    remctl_broker.dispatch(['today'],parsed_args=SimpleNamespace(cmd='today'))\n"
            )
            master_fd, slave_fd = pty.openpty()
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            os.set_blocking(master_fd, False)
            output = bytearray()
            try:
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    readable, _, _ = select.select([master_fd], [], [], 0.1)
                    if readable:
                        try:
                            output.extend(os.read(master_fd, 1024))
                        except (BlockingIOError, OSError):
                            pass
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                for _ in range(16):
                    readable, _, _ = select.select([master_fd], [], [], 0)
                    if not readable:
                        break
                    try:
                        output.extend(os.read(master_fd, 1024))
                    except OSError:
                        break
            finally:
                os.close(master_fd)
            self.assertEqual(process.returncode, 0, bytes(output))
            self.assertEqual(json.loads(merge_marker.read_text()), {"mergeOutput": True})
            self.assertEqual(bytes(output), b"BA")


class DisconnectCancellationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "process-group cancellation is POSIX-only")
    def test_disconnect_after_dispatch_terminates_the_whole_child_group(self):
        with tempfile.TemporaryDirectory() as temp_value:
            pid_file = Path(temp_value) / "grandchild.pid"
            child_script = (
                "import subprocess,sys,time; "
                "from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "Path(sys.argv[1]).write_text(str(child.pid)); "
                "time.sleep(60)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", child_script, str(pid_file)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            server, client = socket.socketpair()

            def disconnect_when_dispatched():
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                client.close()

            closer = threading.Thread(target=disconnect_when_dispatched)
            closer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(remctl_broker._ServerError) as raised:
                    remctl_broker._collect_bounded_output(
                        process,
                        stdin_bytes=b"",
                        stderr_relay_fd=None,
                        timeout=10,
                        client_connection=server,
                    )
            finally:
                server.close()
                closer.join(timeout=1)
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            elapsed = time.monotonic() - started
            self.assertEqual(raised.exception.code, "client_disconnected")
            self.assertTrue(raised.exception.dispatched)
            self.assertTrue(raised.exception.indeterminate)
            self.assertIsNotNone(process.returncode)
            self.assertLess(elapsed, 3)
            self.assertTrue(pid_file.exists())
            grandchild_pid = int(pid_file.read_text())
            survivor_deadline = time.monotonic() + 2
            while time.monotonic() < survivor_deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    break
                proc_stat = Path(f"/proc/{grandchild_pid}/stat")
                if proc_stat.exists() and proc_stat.read_text().split()[2] == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail(f"hosted process-group member {grandchild_pid} survived disconnect")


class SignalControlTests(unittest.TestCase):
    def _control_frame(self, signal_name="SIGINT"):
        payload = json.dumps(
            {
                "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                "operation": "signal",
                "signal": signal_name,
            },
            separators=(",", ":"),
        ).encode()
        return struct.pack("!I", len(payload)) + payload

    def test_partial_and_extra_signal_controls_fail_indeterminate(self):
        payloads = (b"\x00\x00", self._control_frame() + self._control_frame())
        for payload in payloads:
            with self.subTest(payload_length=len(payload)):
                process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                server, client = socket.socketpair()
                client.sendall(payload)
                try:
                    with self.assertRaises(remctl_broker._ServerError) as raised:
                        remctl_broker._collect_bounded_output(
                            process,
                            stdin_bytes=b"",
                            stderr_relay_fd=None,
                            timeout=3,
                            client_connection=server,
                        )
                finally:
                    client.close()
                    server.close()
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                self.assertEqual(raised.exception.code, "invalid_signal_control")
                self.assertTrue(raised.exception.dispatched)
                self.assertTrue(raised.exception.indeterminate)
                self.assertIsNotNone(process.returncode)

    @unittest.skipUnless(os.name == "posix", "POSIX signals are required")
    def test_outer_sigint_forwards_only_sigint_and_reproduces_it(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value).resolve()
            socket_path = root / "capability.sock"
            int_marker = root / "int"
            term_marker = root / "term"
            ready_marker = root / "ready"
            listener = remctl_broker._prepare_socket(socket_path)
            state = {"requests": 0, "child": None, "error": None}

            def serve_one():
                descriptors = []
                try:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(5)
                        request, descriptors = remctl_broker._recv_request(connection)
                        remctl_broker._reject_pipelined_request(connection)
                        state["requests"] += 1
                        self.assertEqual(request["operation"], "run")
                        child_script = (
                            "import signal,time\n"
                            "from pathlib import Path\n"
                            f"int_marker=Path({str(int_marker)!r})\n"
                            f"term_marker=Path({str(term_marker)!r})\n"
                            f"ready_marker=Path({str(ready_marker)!r})\n"
                            "def handle_int(*_):\n"
                            "    int_marker.write_text('INT')\n"
                            "    raise SystemExit(0)\n"
                            "def handle_term(*_):\n"
                            "    term_marker.write_text('TERM')\n"
                            "    raise SystemExit(0)\n"
                            "signal.signal(signal.SIGINT,handle_int)\n"
                            "signal.signal(signal.SIGTERM,handle_term)\n"
                            "ready_marker.write_text('ready')\n"
                            "while True: time.sleep(1)\n"
                        )
                        child = subprocess.Popen(
                            [sys.executable, "-c", child_script],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            start_new_session=True,
                        )
                        state["child"] = child
                        try:
                            exit_code, stdout, stderr, relayed = (
                                remctl_broker._collect_bounded_output(
                                    child,
                                    stdin_bytes=b"",
                                    stderr_relay_fd=None,
                                    timeout=10,
                                    client_connection=connection,
                                )
                            )
                            response = {
                                "status": "completed",
                                "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                                "dispatched": True,
                                "indeterminate": False,
                                "exitCode": exit_code,
                                "stdoutBase64": base64.b64encode(stdout).decode(),
                                "stderrBase64": base64.b64encode(stderr).decode(),
                                "stderrRelayed": relayed,
                            }
                        except Exception as exc:
                            response = remctl_broker._error_response(exc)
                        remctl_broker._send_frame(
                            connection,
                            response,
                            limit=remctl_broker.MAX_RESPONSE_BYTES,
                        )
                except Exception as exc:
                    state["error"] = exc
                finally:
                    for fd in descriptors:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    listener.close()

            server = threading.Thread(target=serve_one)
            server.start()
            client_script = (
                "from types import SimpleNamespace\n"
                "from unittest import mock\n"
                "import remctl_broker\n"
                "from remctl_capabilities import CapabilityBundle\n"
                "with mock.patch.object(remctl_broker,'plan_invocation',"
                "return_value=CapabilityBundle(['today'])):\n"
                "    remctl_broker.dispatch(['today'],parsed_args=SimpleNamespace(cmd='today'),timeout=10)\n"
            )
            environment = os.environ.copy()
            environment[remctl_broker.SOCKET_ENV] = str(socket_path)
            client = subprocess.Popen(
                [sys.executable, "-c", client_script],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 4
                while not ready_marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready_marker.exists())
                os.kill(client.pid, signal.SIGINT)
                client.wait(timeout=7)
                client.communicate(timeout=1)
            finally:
                if client.poll() is None:
                    client.kill()
                    client.wait(timeout=2)
                child = state.get("child")
                if child is not None and child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=2)
                server.join(timeout=2)
            self.assertFalse(server.is_alive())
            self.assertIsNone(state["error"])
            self.assertEqual(state["requests"], 1)
            self.assertEqual(client.returncode, -int(signal.SIGINT))
            self.assertEqual(int_marker.read_text(), "INT")
            self.assertFalse(term_marker.exists())
            self.assertIsNotNone(state["child"].returncode)


class PredispatchCancellationTests(unittest.TestCase):
    def _request(self):
        return {
            "protocolVersion": remctl_broker.PROTOCOL_VERSION,
            "operation": "run",
            "argv": ["add", "Never spawn"],
            "capabilities": [],
            "stdinBase64": "",
            "mergeOutput": False,
            "deadlineEpoch": time.time() + 5,
        }

    def _runtime(self, root):
        return remctl_broker.HostedRuntime(
            app=root / "Host.app",
            runtime=root,
            archive_fd=198,
            cdhash="0" * 40,
            python=Path(sys.executable),
            bridge=root / "bridge",
            private=root / "private",
        )

    def test_queued_signal_eof_and_malformed_control_never_spawn(self):
        cases = ("signal", "eof", "malformed")
        expected_codes = {
            "signal": "client_cancelled_before_dispatch",
            "eof": "client_disconnected",
            "malformed": "invalid_signal_control",
        }
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_value:
                server, client = socket.socketpair()
                marker = Path(temp_value) / "spawned"
                outcome = {}
                started = threading.Event()

                def fake_spawn(*_args, **_kwargs):
                    marker.write_text("spawned")
                    return {}

                def handle():
                    started.set()
                    try:
                        remctl_broker._handle_request(
                            self._request(),
                            descriptors=[],
                            runtime=self._runtime(Path(temp_value)),
                            connection=server,
                        )
                    except Exception as exc:
                        outcome["error"] = exc

                remctl_broker._DISPATCH_LOCK.acquire()
                try:
                    with (
                        mock.patch.object(
                            remctl_broker,
                            "_load_real_parser",
                            return_value=object(),
                        ),
                        mock.patch.object(
                            remctl_broker,
                            "validate_argv",
                            return_value=(["add", "Never spawn"], SimpleNamespace(cmd="add")),
                        ),
                        mock.patch.object(
                            remctl_broker,
                            "validate_received_capabilities",
                            return_value={},
                        ),
                        mock.patch.object(remctl_broker, "validate_capability_bindings"),
                        mock.patch.object(
                            remctl_broker,
                            "_run_cli",
                            side_effect=fake_spawn,
                        ) as spawn,
                    ):
                        worker = threading.Thread(target=handle)
                        worker.start()
                        self.assertTrue(started.wait(timeout=1))
                        time.sleep(0.08)
                        if case == "signal":
                            remctl_broker._send_run_signal_control(
                                client, int(signal.SIGINT)
                            )
                        elif case == "eof":
                            client.close()
                        else:
                            remctl_broker._send_run_signal_control(
                                client, int(signal.SIGINT)
                            )
                            remctl_broker._send_run_signal_control(
                                client, int(signal.SIGTERM)
                            )
                        worker.join(timeout=1)
                        spawn.assert_not_called()
                finally:
                    remctl_broker._DISPATCH_LOCK.release()
                    if client.fileno() >= 0:
                        client.close()
                    server.close()
                self.assertFalse(worker.is_alive())
                self.assertFalse(marker.exists())
                error = outcome.get("error")
                self.assertIsInstance(error, remctl_broker._ServerError)
                self.assertEqual(error.code, expected_codes[case])
                response = remctl_broker._error_response(error)
                self.assertFalse(response["dispatched"])
                self.assertFalse(response["indeterminate"])

    def test_cancellation_during_slow_prespawn_work_wins_the_launch_gate(self):
        expected_codes = {
            "signal": "client_cancelled_before_dispatch",
            "eof": "client_disconnected",
            "malformed": "invalid_signal_control",
        }
        for case in expected_codes:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_value:
                server, client = socket.socketpair()
                root = Path(temp_value)
                marker = root / "spawned"
                verification_started = threading.Event()
                release_verification = threading.Event()
                outcome = {}

                def slow_verification(_runtime):
                    verification_started.set()
                    self.assertTrue(release_verification.wait(timeout=2))

                def fake_popen(*_args, **_kwargs):
                    marker.write_text("spawned")
                    raise AssertionError("Popen must not run after predispatch cancellation")

                def run():
                    try:
                        remctl_broker._run_cli(
                            ["add", "Never spawn"],
                            runtime=self._runtime(root),
                            capabilities={},
                            stdin_bytes=b"",
                            timeout=5,
                            client_connection=server,
                        )
                    except Exception as exc:
                        outcome["error"] = exc

                with (
                    mock.patch.object(
                        remctl_broker,
                        "_verify_sealed_bundle",
                        side_effect=slow_verification,
                    ),
                    mock.patch.object(
                        remctl_broker,
                        "_load_real_parser",
                        return_value=object(),
                    ),
                    mock.patch.object(
                        remctl_broker,
                        "validate_argv",
                        return_value=(["add", "Never spawn"], SimpleNamespace(cmd="add")),
                    ),
                    mock.patch.object(
                        remctl_broker,
                        "materialize_inputs",
                        return_value=(["add", "Never spawn"], None, None, None),
                    ),
                    mock.patch.object(
                        remctl_broker.subprocess,
                        "Popen",
                        side_effect=fake_popen,
                    ) as popen,
                ):
                    worker = threading.Thread(target=run)
                    worker.start()
                    self.assertTrue(verification_started.wait(timeout=1))
                    if case == "signal":
                        remctl_broker._send_run_signal_control(
                            client, int(signal.SIGINT)
                        )
                    elif case == "eof":
                        client.close()
                    else:
                        remctl_broker._send_run_signal_control(
                            client, int(signal.SIGINT)
                        )
                        remctl_broker._send_run_signal_control(
                            client, int(signal.SIGTERM)
                        )
                    time.sleep(0.05)
                    release_verification.set()
                    worker.join(timeout=2)
                    popen.assert_not_called()
                if client.fileno() >= 0:
                    client.close()
                server.close()
                self.assertFalse(worker.is_alive())
                self.assertFalse(marker.exists())
                error = outcome.get("error")
                self.assertIsInstance(error, remctl_broker._ServerError)
                self.assertEqual(error.code, expected_codes[case])
                response = remctl_broker._error_response(error)
                self.assertFalse(response["dispatched"])
                self.assertFalse(response["indeterminate"])


class InteractiveRelayTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "openpty"), "PTY support is required")
    def test_stderr_prompt_is_relayed_before_terminal_answer(self):
        master_fd, slave_fd = pty.openpty()
        seen = bytearray()
        answered = threading.Event()
        stop_reader = threading.Event()

        def answer_prompt():
            deadline = time.monotonic() + 4
            while not stop_reader.is_set() and time.monotonic() < deadline:
                readable, _, _ = select.select([master_fd], [], [], 0.1)
                if readable:
                    try:
                        seen.extend(os.read(master_fd, 1024))
                    except OSError:
                        return
                if b"Continue? " in seen and not answered.is_set():
                    try:
                        os.write(master_fd, b"y\n")
                    except OSError:
                        return
                    answered.set()

        reader = threading.Thread(target=answer_prompt, daemon=True)
        reader.start()
        code = (
            "import sys; "
            "sys.stderr.write('Continue? '); sys.stderr.flush(); "
            "answer=sys.stdin.readline(); "
            "sys.stderr.write('accepted\\n' if answer.strip() == 'y' else 'rejected\\n')"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        try:
            exit_code, stdout, stderr, relayed = remctl_broker._collect_bounded_output(
                process,
                stdin_bytes=b"",
                stderr_relay_fd=slave_fd,
                timeout=5,
            )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            stop_reader.set()
            reader.join(timeout=1)
            os.close(master_fd)
            os.close(slave_fd)
        self.assertFalse(reader.is_alive())
        self.assertTrue(answered.is_set())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr, b"Continue? accepted\n")
        self.assertTrue(relayed)


class FramingSecurityTests(unittest.TestCase):
    def test_short_send_preserves_frame_and_delivers_descriptor_once(self):
        sender, receiver = socket.socketpair()
        read_fd, write_fd = os.pipe()
        received = []

        class ShortSender:
            def sendmsg(self, buffers, ancillary):
                return sender.sendmsg([buffers[0][:7]], ancillary)

            def sendall(self, remainder):
                sender.sendall(remainder)

        payload = {"protocolVersion": remctl_broker.PROTOCOL_VERSION, "operation": "ping"}
        try:
            remctl_broker._send_frame(
                ShortSender(), payload, limit=1024, descriptors=[read_fd]
            )
            decoded, received = remctl_broker._recv_request(receiver)
            self.assertEqual(decoded, payload)
            self.assertEqual(len(received), 1)
            self.assertEqual(os.fstat(received[0]), os.fstat(read_fd))
        finally:
            for fd in received:
                os.close(fd)
            os.close(read_fd)
            os.close(write_fd)
            sender.close()
            receiver.close()

    def test_symlinked_socket_parent_is_rejected_by_client_and_server(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value).resolve()
            actual_parent = root / "actual"
            actual_parent.mkdir(mode=0o700)
            link = root / "linked"
            link.symlink_to(actual_parent, target_is_directory=True)
            path = link / "capability.sock"
            metadata = remctl_broker._socket_metadata(path)
            self.assertFalse(metadata["secure"])
            self.assertIn("canonical", metadata["error"])
            with self.assertRaises(remctl_broker._ServerError):
                remctl_broker._prepare_socket(path)

    def test_receive_deadline_is_monotonic_across_slow_drip(self):
        class SlowConnection:
            def __init__(self):
                self.remaining = bytearray(b"abc")
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

            def recv(self, _size):
                time.sleep(0.03)
                if not self.remaining:
                    return b""
                return bytes([self.remaining.pop(0)])

        connection = SlowConnection()
        with self.assertRaises(socket.timeout):
            remctl_broker._recv_exact(
                connection,
                3,
                deadline=time.monotonic() + 0.05,
            )
        self.assertGreaterEqual(len(connection.timeouts), 2)
        self.assertGreater(connection.timeouts[0], connection.timeouts[-1])

    def test_second_pipelined_frame_is_rejected_before_dispatch(self):
        sender, receiver = socket.socketpair()
        payload = json.dumps(
            {"protocolVersion": remctl_broker.PROTOCOL_VERSION, "operation": "ping"},
            separators=(",", ":"),
        ).encode()
        frame = struct.pack("!I", len(payload)) + payload
        try:
            sender.sendall(frame + frame)
            request, descriptors = remctl_broker._recv_request(receiver)
            self.assertEqual(request["operation"], "ping")
            self.assertEqual(descriptors, [])
            with self.assertRaises(remctl_broker._ServerError):
                remctl_broker._reject_pipelined_request(receiver)
        finally:
            sender.close()
            receiver.close()

    def test_truncated_rights_are_closed_without_fd_growth(self):
        baseline = len(os.listdir("/dev/fd"))
        payload = json.dumps(
            {"protocolVersion": remctl_broker.PROTOCOL_VERSION, "operation": "ping"},
            separators=(",", ":"),
        ).encode()
        frame = struct.pack("!I", len(payload)) + payload
        for _ in range(5):
            sender, receiver = socket.socketpair()
            sent_fds = [os.open("/dev/null", os.O_RDONLY) for _ in range(72)]
            try:
                rights = array.array("i", sent_fds)
                sender.sendmsg(
                    [frame],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)],
                )
            finally:
                for fd in sent_fds:
                    os.close(fd)
                sender.close()
            try:
                with self.assertRaises(remctl_broker._ServerError):
                    remctl_broker._recv_request(receiver)
            finally:
                receiver.close()
            self.assertEqual(len(os.listdir("/dev/fd")), baseline)


class PeerCredentialTests(unittest.TestCase):
    class _Connection:
        def __init__(self, payload):
            self.payload = payload

        def getsockopt(self, level, option, size):
            if (level, option, size) != (0, 1, 128):
                raise AssertionError((level, option, size))
            return self.payload

    def _peer_uid(self, payload):
        with (
            mock.patch.object(remctl_broker.sys, "platform", "darwin"),
            mock.patch.object(remctl_broker.socket, "LOCAL_PEERCRED", 1, create=True),
        ):
            return remctl_broker._peer_uid(self._Connection(payload))

    def test_darwin_peer_uid_accepts_current_user(self):
        payload = struct.pack("=II", 0, os.getuid()) + b"\0" * 8
        self.assertEqual(self._peer_uid(payload), os.getuid())

    def test_darwin_peer_uid_rejects_short_and_unknown_version(self):
        with self.assertRaises(remctl_broker._ServerError):
            self._peer_uid(b"short")
        with self.assertRaises(remctl_broker._ServerError):
            self._peer_uid(struct.pack("=II", 1, os.getuid()) + b"\0" * 8)

    def test_darwin_peer_uid_exposes_foreign_uid_for_server_rejection(self):
        foreign = os.getuid() + 1
        payload = struct.pack("=II", 0, foreign) + b"\0" * 8
        self.assertEqual(self._peer_uid(payload), foreign)


class PostSpawnClassificationTests(unittest.TestCase):
    class _FakeProcess:
        pid = 424242

        def poll(self):
            return 0

    class _FailingDiscardSet(set):
        def discard(self, _item):
            raise RuntimeError("cleanup failed")

    def _runtime(self, root):
        return remctl_broker.HostedRuntime(
            app=root / "Host.app",
            runtime=root,
            archive_fd=198,
            cdhash="0" * 40,
            python=Path(sys.executable),
            bridge=root / "bridge",
            private=root / "private",
        )

    def _enter_common_patches(self, stack, process):
        patches = (
            mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
            mock.patch.object(remctl_broker, "_load_real_parser", return_value=object()),
            mock.patch.object(remctl_broker, "validate_argv", return_value=(["today"], object())),
            mock.patch.object(
                remctl_broker,
                "materialize_inputs",
                return_value=(["today"], None, None, None),
            ),
            mock.patch.object(remctl_broker, "_service_environment", return_value={}),
            mock.patch.object(remctl_broker.subprocess, "Popen", return_value=process),
        )
        for patcher in patches:
            stack.enter_context(patcher)

    def _assert_indeterminate(self, raised):
        self.assertTrue(raised.exception.dispatched)
        self.assertTrue(raised.exception.indeterminate)
        self.assertFalse(raised.exception.retry_safe if hasattr(raised.exception, "retry_safe") else False)

    def test_timeout_and_output_limit_preserve_bounded_partial_output(self):
        timeout_process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys,time; print('before timeout', flush=True); "
                "print('timeout detail', file=sys.stderr, flush=True); time.sleep(5)",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with self.assertRaises(remctl_broker._ServerError) as timeout_error:
            remctl_broker._collect_bounded_output(
                timeout_process,
                stdin_bytes=b"",
                stderr_relay_fd=None,
                timeout=0.1,
            )
        self.assertEqual(timeout_error.exception.code, "command_timeout")
        self.assertEqual(timeout_error.exception.stdout, b"before timeout\n")
        self.assertEqual(timeout_error.exception.stderr, b"timeout detail\n")

        limit_process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.write('12345'); sys.stdout.flush()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with (
            mock.patch.object(remctl_broker, "MAX_STDOUT_BYTES", 4),
            mock.patch.object(remctl_broker, "MAX_COMBINED_OUTPUT_BYTES", 4),
            self.assertRaises(remctl_broker._ServerError) as limit_error,
        ):
            remctl_broker._collect_bounded_output(
                limit_process,
                stdin_bytes=b"",
                stderr_relay_fd=None,
                timeout=1,
            )
        self.assertEqual(limit_error.exception.code, "command_output_too_large")
        self.assertEqual(limit_error.exception.stdout, b"1234")

    def test_collect_fault_after_spawn_is_indeterminate(self):
        process = self._FakeProcess()
        with tempfile.TemporaryDirectory() as temp_value:
            with contextlib.ExitStack() as stack:
                self._enter_common_patches(stack, process)
                stack.enter_context(mock.patch.object(
                    remctl_broker,
                    "_collect_bounded_output",
                    side_effect=RuntimeError("collect failed"),
                ))
                with self.assertRaises(remctl_broker._ServerError) as raised:
                    remctl_broker._run_cli(
                        ["today"],
                        runtime=self._runtime(Path(temp_value)),
                        capabilities={},
                        stdin_bytes=b"",
                        timeout=1,
                    )
        self._assert_indeterminate(raised)

    def test_response_encoding_fault_after_spawn_is_indeterminate(self):
        process = self._FakeProcess()
        with tempfile.TemporaryDirectory() as temp_value:
            with contextlib.ExitStack() as stack:
                self._enter_common_patches(stack, process)
                stack.enter_context(mock.patch.object(
                    remctl_broker,
                    "_collect_bounded_output",
                    return_value=(0, b"out", b"err", False),
                ))
                stack.enter_context(mock.patch.object(
                    remctl_broker.base64,
                    "b64encode",
                    side_effect=RuntimeError("encode failed"),
                ))
                with self.assertRaises(remctl_broker._ServerError) as raised:
                    remctl_broker._run_cli(
                        ["today"],
                        runtime=self._runtime(Path(temp_value)),
                        capabilities={},
                        stdin_bytes=b"",
                        timeout=1,
                    )
        self._assert_indeterminate(raised)

    def test_cleanup_fault_after_spawn_is_indeterminate(self):
        process = self._FakeProcess()
        active = self._FailingDiscardSet()
        with tempfile.TemporaryDirectory() as temp_value:
            with contextlib.ExitStack() as stack:
                self._enter_common_patches(stack, process)
                stack.enter_context(mock.patch.object(
                    remctl_broker,
                    "_collect_bounded_output",
                    return_value=(0, b"", b"", False),
                ))
                stack.enter_context(
                    mock.patch.object(remctl_broker, "_ACTIVE_PROCESSES", active)
                )
                with self.assertRaises(remctl_broker._ServerError) as raised:
                    remctl_broker._run_cli(
                        ["today"],
                        runtime=self._runtime(Path(temp_value)),
                        capabilities={},
                        stdin_bytes=b"",
                        timeout=1,
                    )
        self._assert_indeterminate(raised)


class SignatureStatusTests(unittest.TestCase):
    def test_signature_status_has_structured_identity(self):
        verify = subprocess.CompletedProcess([], 0, b"", b"")
        describe = subprocess.CompletedProcess(
            [],
            0,
            (
                b"designated => identifier net.macstories.remctl.capability-host\n"
            ),
            (
                b"Identifier=net.macstories.remctl.capability-host\n"
                b"TeamIdentifier=TEAM123\n"
                b"CDHash=0123456789abcdef0123456789abcdef01234567\n"
            ),
        )
        with mock.patch.object(subprocess, "run", side_effect=[verify, describe]):
            status = remctl_broker._signature_status(Path("/Applications/Host.app"))
        self.assertTrue(status["valid"])
        self.assertEqual(status["teamID"], "TEAM123")
        self.assertEqual(status["cdhash"], "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(
            status["designatedRequirement"],
            "identifier net.macstories.remctl.capability-host",
        )


class PingTests(unittest.TestCase):
    def test_ping_uses_transport_only_operation_and_caller_timeout(self):
        response = {"status": "ok", "protocolVersion": remctl_broker.PROTOCOL_VERSION}
        with mock.patch.object(remctl_broker, "_request", return_value=response) as request:
            self.assertEqual(remctl_broker.ping(timeout=7), response)
        request.assert_called_once_with(
            {
                "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                "operation": "ping",
            },
            timeout=7,
        )

    def test_ping_rejects_stale_or_extended_responses(self):
        invalid = (
            {"status": "ok", "protocolVersion": remctl_broker.PROTOCOL_VERSION + 1},
            {
                "status": "ok",
                "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                "unexpected": True,
            },
        )
        for response in invalid:
            with self.subTest(response=response), mock.patch.object(
                remctl_broker,
                "_request",
                return_value=response,
            ), self.assertRaises(remctl_broker.HostUnhealthy):
                remctl_broker.ping()


class NativePermissionPromptContractTests(unittest.TestCase):
    def test_persistent_native_server_owns_permission_requests_on_the_appkit_loop(self):
        source = (
            Path(__file__).resolve().parents[1] / "remctl-capability-host.swift"
        ).read_text(encoding="utf-8")
        self.assertIn("class CapabilityHostApplicationDelegate", source)
        self.assertIn("class NativePermissionServer", source)
        self.assertIn("func makeNativeSocketPair", source)
        self.assertIn("private let nativeDescriptor: Int32 = 199", source)
        self.assertIn(
            'private let nativeFDEnvironment = "REMCTL_CAPABILITY_NATIVE_FD"',
            source,
        )
        self.assertIn("class PermissionPromptController", source)
        self.assertIn("func applicationDidFinishLaunching", source)
        self.assertIn("spawnArchivedPython(", source)
        self.assertIn("nativeChannel:", source)
        self.assertIn("permissionStatusPayload()", source)
        self.assertIn(
            '["permissionStatus", "requestReminders", "requestAutomation"]',
            source,
        )
        self.assertIn("DispatchQueue.main.async", source)
        self.assertIn("automationQueue.async", source)
        self.assertIn("withExtendedLifetime(delegate)", source)
        self.assertIn("application.run()", source)
        self.assertIn("window?.makeKeyAndOrderFront(nil)", source)
        self.assertIn("window.orderFrontRegardless()", source)
        self.assertIn(
            "guard requestGate.beginIfReady(currentReadiness()) else {",
            source,
        )
        self.assertIn("applicationActive: NSApp.isActive", source)
        self.assertIn("runningApplicationActive: running.isActive", source)
        self.assertIn(
            "NSWorkspace.shared.frontmostApplication?.processIdentifier == getpid()",
            source,
        )
        self.assertIn(
            "windowVisible: permissionWindow?.isVisible == true",
            source,
        )
        self.assertIn("windowKey: permissionWindow?.isKeyWindow == true", source)
        self.assertIn("permissionWindow?.close()", source)
        self.assertIn(
            "remindersRequestResult(granted: granted, error: error)",
            source,
        )
        self.assertIn("if let error { return eventKitErrorStatus(error) }", source)
        self.assertIn("String(value.domain.prefix(80))", source)
        self.assertIn(
            "effectiveStatus = automationStatusCache.recordPermissionResult(status)",
            source,
        )
        self.assertNotIn("automationStatusCache.recordAuthoritative", source)
        self.assertNotIn(
            "finish(with: automationAuthorizationStatus(askUser: true))",
            source,
        )

    def test_broker_uses_only_the_persistent_native_permission_channel(self):
        broker_source = (
            Path(__file__).resolve().parents[1] / "remctl_broker.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(remctl_broker.NATIVE_PERMISSION_FD, 199)
        self.assertEqual(remctl_broker.NATIVE_PROTOCOL_VERSION, 1)
        self.assertIn('NATIVE_FD_ENV = "REMCTL_CAPABILITY_NATIVE_FD"', broker_source)
        self.assertNotIn('"--permission-status"', broker_source)
        self.assertNotIn('"--request-reminders"', broker_source)
        self.assertNotIn('"--request-automation"', broker_source)

    def test_broker_surfaces_terminal_native_prompt_outcomes(self):
        outcomes = {
            "cancelled": ("permission_prompt_cancelled", "was cancelled"),
            "timedOut": ("permission_prompt_timed_out", "timed out"),
            "promptUnavailable": (
                "permission_prompt_unavailable",
                "reminders permission prompt",
            ),
        }
        for outcome, (expected_code, expected_message) in outcomes.items():
            with self.subTest(outcome=outcome):
                response = {
                    "status": "ok",
                    "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                    "permissions": {
                        "fullDiskAccess": "authorized",
                        "reminders": outcome,
                        "automation": "notDetermined",
                    },
                }
                with mock.patch.object(remctl_broker, "_request", return_value=response):
                    with self.assertRaises(remctl_broker.CapabilityHostError) as raised:
                        remctl_broker.request_permission("reminders")
                self.assertEqual(raised.exception.code, expected_code)
                self.assertIn(expected_message, str(raised.exception))

    def test_broker_returns_denied_and_restricted_permission_statuses(self):
        for outcome in ("denied", "restricted"):
            with self.subTest(outcome=outcome):
                response = {
                    "status": "ok",
                    "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                    "permissions": {
                        "fullDiskAccess": "authorized",
                        "reminders": outcome,
                        "automation": "notDetermined",
                    },
                }
                with mock.patch.object(remctl_broker, "_request", return_value=response):
                    permissions = remctl_broker.request_permission("reminders")
                self.assertEqual(permissions["reminders"], outcome)


class PermissionRequestLifecycleTests(unittest.TestCase):
    def setUp(self):
        remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.clear()
        self.full_disk = mock.patch.object(
            remctl_broker,
            "_full_disk_access_status",
            return_value="authorized",
        )
        self.full_disk.start()
        with remctl_broker._PERMISSION_STATUS_LOCK:
            remctl_broker._PERMISSION_STATUS_CACHE.clear()
            remctl_broker._PERMISSION_STATUS_GENERATION.clear()
            remctl_broker._PERMISSION_STATUS_REFRESHES.clear()
            remctl_broker._PRIVATE_PROTOCOL_CACHE.clear()
            remctl_broker._PERMISSION_STATUS_FAILURES.clear()

    def tearDown(self):
        self.full_disk.stop()
        remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.clear()
        with remctl_broker._PERMISSION_STATUS_LOCK:
            remctl_broker._PERMISSION_STATUS_CACHE.clear()
            remctl_broker._PERMISSION_STATUS_GENERATION.clear()
            remctl_broker._PERMISSION_STATUS_REFRESHES.clear()
            remctl_broker._PRIVATE_PROTOCOL_CACHE.clear()
            remctl_broker._PERMISSION_STATUS_FAILURES.clear()

    def _runtime(self, root):
        app = root / "RemCTL Capability Host.app"
        executable = app / "Contents/MacOS" / remctl_broker.EXECUTABLE_NAME
        executable.parent.mkdir(parents=True)
        executable.touch()
        runtime = app / "Contents/Resources/CapabilityRuntime"
        runtime.mkdir(parents=True)
        return remctl_broker.HostedRuntime(
            app=app,
            runtime=runtime,
            archive_fd=198,
            cdhash="0" * 40,
            python=Path(sys.executable),
            bridge=runtime / "bin/remctl-bridge",
            private=runtime / "bin/remctl-private",
        )

    def _permissions(self):
        return {
            "status": "ok",
            "fullDiskAccess": "authorized",
            "reminders": "cancelled",
            "automation": "notDetermined",
            "automationTarget": "com.apple.reminders",
        }

    def test_disk_access_probe_does_not_block_permission_cache_updates(self):
        for initial_cache in (True, False):
            with self.subTest(initial_cache=initial_cache), tempfile.TemporaryDirectory() as value:
                runtime = self._runtime(Path(value))
                permissions = self._permissions()
                probe_started = threading.Event()
                release_probe = threading.Event()
                updated = threading.Event()
                results = []
                errors = []
                refreshed = threading.Event()
                refreshed.set()

                def refresh(_runtime):
                    remctl_broker._remember_permission_status(runtime, permissions)
                    return refreshed

                def probe():
                    probe_started.set()
                    if not release_probe.wait(timeout=2):
                        raise AssertionError("disk access probe was not released")
                    return "authorized"

                def snapshot():
                    try:
                        results.append(remctl_broker._permission_status_snapshot(runtime))
                    except BaseException as exc:
                        errors.append(exc)

                def update():
                    remctl_broker._remember_permission_status(
                        runtime, {**permissions, "reminders": "authorized"}
                    )
                    updated.set()

                if initial_cache:
                    remctl_broker._remember_permission_status(runtime, permissions)
                with (
                    mock.patch.object(remctl_broker, "_full_disk_access_status", side_effect=probe),
                    mock.patch.object(remctl_broker, "_schedule_permission_status_refresh", side_effect=refresh),
                ):
                    reader = threading.Thread(target=snapshot)
                    updater = threading.Thread(target=update)
                    reader.start()
                    try:
                        self.assertTrue(probe_started.wait(timeout=1))
                        updater.start()
                        self.assertTrue(updated.wait(timeout=1))
                    finally:
                        release_probe.set()
                        reader.join(timeout=2)
                        if updater.ident is not None:
                            updater.join(timeout=2)
                self.assertFalse(reader.is_alive())
                self.assertFalse(updater.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [permissions])

    def test_precancelled_client_never_dispatches_or_restarts_host(self):
        for cancellation, expected_code in (
            ("eof", "client_disconnected"),
            ("protocol", "invalid_frame"),
        ):
            with self.subTest(cancellation=cancellation), tempfile.TemporaryDirectory() as value:
                runtime = self._runtime(Path(value))
                server, client = socket.socketpair()
                if cancellation == "eof":
                    client.close()
                else:
                    client.sendall(b"unexpected")
                with (
                    mock.patch.object(remctl_broker, "_verified_parent_host_pid") as verify_parent,
                    mock.patch.object(remctl_broker, "_native_permission_command") as native,
                    mock.patch.object(remctl_broker.os, "kill") as kill,
                    self.assertRaises(remctl_broker._ServerError) as raised,
                ):
                    remctl_broker._request_native_permission_with_client_lifecycle(
                        runtime,
                        "requestReminders",
                        server,
                    )
                if client.fileno() >= 0:
                    client.close()
                server.close()
                self.assertEqual(raised.exception.code, expected_code)
                self.assertFalse(raised.exception.dispatched)
                self.assertFalse(raised.exception.indeterminate)
                verify_parent.assert_not_called()
                native.assert_not_called()
                kill.assert_not_called()

    def test_disconnect_racing_before_dispatch_wins_without_restart(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            server, client = socket.socketpair()
            verification_started = threading.Event()
            release_verification = threading.Event()
            outcome = {}

            def slow_parent_verification(_runtime):
                verification_started.set()
                self.assertTrue(release_verification.wait(timeout=2))
                return 4242

            def run_request():
                try:
                    remctl_broker._request_native_permission_with_client_lifecycle(
                        runtime,
                        "requestReminders",
                        server,
                    )
                except Exception as exc:
                    outcome["error"] = exc

            with (
                mock.patch.object(
                    remctl_broker,
                    "_verified_parent_host_pid",
                    side_effect=slow_parent_verification,
                ),
                mock.patch.object(remctl_broker, "_native_permission_command") as native,
                mock.patch.object(remctl_broker.os, "kill") as kill,
            ):
                worker = threading.Thread(target=run_request)
                worker.start()
                self.assertTrue(verification_started.wait(timeout=1))
                client.close()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and any(
                    thread.name.startswith("remctl-permission-client-")
                    for thread in threading.enumerate()
                ):
                    time.sleep(0.01)
                self.assertFalse(any(
                    thread.name.startswith("remctl-permission-client-")
                    for thread in threading.enumerate()
                ))
                release_verification.set()
                worker.join(timeout=1)
            server.close()
        self.assertFalse(worker.is_alive())
        error = outcome.get("error")
        self.assertIsInstance(error, remctl_broker._ServerError)
        self.assertEqual(error.code, "client_disconnected")
        self.assertFalse(error.dispatched)
        self.assertFalse(error.indeterminate)
        native.assert_not_called()
        kill.assert_not_called()

    def test_disconnect_or_protocol_loss_terminates_exact_parent_once(self):
        for disconnect in ("eof", "protocol"):
            with self.subTest(disconnect=disconnect), tempfile.TemporaryDirectory() as value:
                runtime = self._runtime(Path(value))
                expected_executable = (
                    runtime.app / "Contents/MacOS" / remctl_broker.EXECUTABLE_NAME
                ).resolve(strict=True)
                server, client = socket.socketpair()
                native_started = threading.Event()
                release_native = threading.Event()
                result = {}

                def native_request(*_args, **_kwargs):
                    native_started.set()
                    self.assertTrue(release_native.wait(timeout=2))
                    return self._permissions()

                def run_request():
                    result["permissions"] = (
                        remctl_broker._request_native_permission_with_client_lifecycle(
                            runtime,
                            "requestReminders",
                            server,
                        )
                    )

                with (
                    mock.patch.object(
                        remctl_broker,
                        "_verified_parent_host_pid",
                        return_value=4242,
                    ),
                    mock.patch.object(
                        remctl_broker,
                        "_process_executable_path",
                        return_value=expected_executable,
                    ),
                    mock.patch.object(remctl_broker.os, "getppid", return_value=4242),
                    mock.patch.object(remctl_broker.os, "kill") as kill,
                    mock.patch.object(
                        remctl_broker,
                        "_native_permission_command",
                        side_effect=native_request,
                    ),
                ):
                    worker = threading.Thread(target=run_request)
                    worker.start()
                    self.assertTrue(native_started.wait(timeout=1))
                    if disconnect == "eof":
                        client.close()
                    else:
                        client.sendall(b"unexpected")
                    deadline = time.monotonic() + 1
                    while not kill.called and time.monotonic() < deadline:
                        time.sleep(0.01)
                    kill.assert_called_once_with(4242, signal.SIGTERM)
                    release_native.set()
                    worker.join(timeout=1)
                if client.fileno() >= 0:
                    client.close()
                server.close()
                self.assertFalse(worker.is_alive())
                self.assertEqual(result["permissions"], self._permissions())
                self.assertFalse(any(
                    thread.name.startswith("remctl-permission-client-")
                    for thread in threading.enumerate()
                ))

    def test_normal_permission_completion_stops_monitor_without_restart(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            server, client = socket.socketpair()
            with (
                mock.patch.object(
                    remctl_broker,
                    "_verified_parent_host_pid",
                    return_value=4242,
                ),
                mock.patch.object(
                    remctl_broker,
                    "_native_permission_command",
                    return_value=self._permissions(),
                ),
                mock.patch.object(
                    remctl_broker,
                    "_terminate_verified_parent_host",
                ) as terminate,
            ):
                permissions = remctl_broker._request_native_permission_with_client_lifecycle(
                    runtime,
                    "requestReminders",
                    server,
                )
            client.close()
            server.close()
        self.assertEqual(permissions, self._permissions())
        terminate.assert_not_called()
        self.assertFalse(any(
            thread.name.startswith("remctl-permission-client-")
            for thread in threading.enumerate()
        ))

    def test_permission_status_does_not_start_disconnect_restart_monitor(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            server, client = socket.socketpair()
            request = {
                "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                "operation": "permissionStatus",
            }
            client.close()
            permissions = self._permissions()
            with (
                mock.patch.object(
                    remctl_broker,
                    "_permission_status_snapshot",
                    return_value=permissions,
                ),
                mock.patch.object(
                    remctl_broker,
                    "_private_protocol_snapshot",
                    return_value={"compatible": True, "version": 2},
                ),
                mock.patch.object(
                    remctl_broker,
                    "_native_permission_command",
                ) as native,
                mock.patch.object(remctl_broker, "_verified_parent_host_pid") as verify_parent,
                mock.patch.object(remctl_broker.os, "kill") as kill,
            ):
                response = remctl_broker._handle_request(
                    request,
                    descriptors=[],
                    runtime=runtime,
                    connection=server,
                )
            server.close()
        self.assertEqual(response["permissions"], permissions)
        native.assert_not_called()
        verify_parent.assert_not_called()
        kill.assert_not_called()

    def test_explicit_permission_result_updates_bounded_status_cache(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            server, client = socket.socketpair()
            request = {
                "protocolVersion": remctl_broker.PROTOCOL_VERSION,
                "operation": "requestPermission",
                "permission": "reminders",
            }
            permissions = self._permissions()
            with mock.patch.object(
                remctl_broker,
                "_request_native_permission_with_client_lifecycle",
                return_value=permissions,
            ):
                remctl_broker._handle_request(
                    request,
                    descriptors=[],
                    runtime=runtime,
                    connection=server,
                )
            snapshot = remctl_broker._permission_status_snapshot(runtime)
            client.close()
            server.close()
        self.assertEqual(snapshot, permissions)

    def test_server_status_never_uses_native_permission_preflight(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            permissions = self._permissions()
            with (
                mock.patch.object(
                    remctl_broker,
                    "_permission_status_snapshot",
                    return_value=permissions,
                ),
                mock.patch.object(
                    remctl_broker,
                    "_private_protocol_snapshot",
                    return_value={"compatible": True, "version": 2},
                ),
                mock.patch.object(
                    remctl_broker,
                    "validate_command_scope_for_server",
                    return_value={"local": [], "hosted": []},
                ),
            ):
                status = remctl_broker._server_status(runtime)
        self.assertTrue(status["available"])
        self.assertTrue(status["ready"])

    def test_delayed_signature_verification_prevents_early_helper_execution(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            verification_started = threading.Event()
            release_verification = threading.Event()

            def verify(_runtime):
                verification_started.set()
                self.assertTrue(release_verification.wait(timeout=2))
                return 4242

            with (
                mock.patch.object(
                    remctl_broker,
                    "_verified_parent_host_pid",
                    side_effect=verify,
                ),
                mock.patch.object(
                    remctl_broker,
                    "_native_status_after_verification",
                    return_value=self._permissions(),
                ),
                mock.patch.object(
                    remctl_broker,
                    "_private_protocol",
                    return_value={"compatible": True, "version": 2},
                ) as private_protocol,
                mock.patch.object(
                    remctl_broker,
                    "validate_command_scope_for_server",
                    return_value={"local": [], "hosted": []},
                ),
            ):
                status = remctl_broker._server_status(runtime)
                self.assertTrue(verification_started.is_set())
                private_protocol.assert_not_called()
                self.assertIn("pending", status["privateProtocol"]["error"])
                release_verification.set()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with remctl_broker._PERMISSION_STATUS_LOCK:
                        if not remctl_broker._PERMISSION_STATUS_REFRESHES:
                            break
                    time.sleep(0.01)
                private_protocol.assert_called_once_with(runtime)

    def test_resign_or_tamper_is_detected_before_next_helper_execution(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            signature_error = remctl_broker._ServerError(
                "signed host verification failed",
                code="host_signature_invalid",
            )
            with (
                mock.patch.object(
                    remctl_broker,
                    "_verified_parent_host_pid",
                    side_effect=(4242, signature_error),
                ),
                mock.patch.object(
                    remctl_broker,
                    "_native_status_after_verification",
                    return_value=self._permissions(),
                ),
                mock.patch.object(
                    remctl_broker,
                    "_private_protocol",
                    return_value={"compatible": True, "version": 2},
                ) as private_protocol,
            ):
                first = remctl_broker._schedule_permission_status_refresh(runtime)
                self.assertTrue(first.wait(timeout=1))
                private_protocol.assert_called_once_with(runtime)
                key = remctl_broker._permission_cache_key(runtime)
                with remctl_broker._PERMISSION_STATUS_LOCK:
                    permission_time, permissions = remctl_broker._PERMISSION_STATUS_CACHE[key]
                    private_time, private = remctl_broker._PRIVATE_PROTOCOL_CACHE[key]
                    self.assertGreater(permission_time, 0)
                    self.assertGreater(private_time, 0)
                    remctl_broker._PERMISSION_STATUS_CACHE[key] = (0.0, permissions)
                    remctl_broker._PRIVATE_PROTOCOL_CACHE[key] = (0.0, private)
                second = remctl_broker._schedule_permission_status_refresh(runtime)
                self.assertTrue(second.wait(timeout=1))
                private_protocol.assert_called_once_with(runtime)
                private = remctl_broker._private_protocol_snapshot(runtime)
        self.assertFalse(private["compatible"])
        self.assertIn("verification failed", private["error"])
        self.assertFalse(remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.is_set())

    def test_stale_verified_status_stays_ready_until_reverification_fails(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            runtime.bridge.parent.mkdir(parents=True, exist_ok=True)
            runtime.bridge.touch()
            key = remctl_broker._permission_cache_key(runtime)
            authorized = {
                **self._permissions(),
                "reminders": "authorized",
                "automation": "authorized",
            }
            with remctl_broker._PERMISSION_STATUS_LOCK:
                remctl_broker._PERMISSION_STATUS_CACHE[key] = (
                    0.0,
                    authorized,
                )
                remctl_broker._PRIVATE_PROTOCOL_CACHE[key] = (
                    0.0,
                    {"compatible": True, "version": 2},
                )
            verification_started = threading.Event()
            release_verification = threading.Event()
            signature_error = remctl_broker._ServerError(
                "signed host verification failed",
                code="host_signature_invalid",
            )

            def fail_verification(_runtime):
                verification_started.set()
                self.assertTrue(release_verification.wait(timeout=2))
                raise signature_error

            with (
                mock.patch.object(
                    remctl_broker,
                    "_verified_parent_host_pid",
                    side_effect=fail_verification,
                ),
                mock.patch.object(
                    remctl_broker,
                    "_private_protocol",
                ) as private_protocol,
                mock.patch.object(
                    remctl_broker,
                    "validate_command_scope_for_server",
                    return_value={"local": [], "hosted": []},
                ),
            ):
                stale = remctl_broker._server_status(runtime)
                self.assertTrue(verification_started.is_set())
                self.assertTrue(stale["fullReady"])
                private_protocol.assert_not_called()
                release_verification.set()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with remctl_broker._PERMISSION_STATUS_LOCK:
                        if not remctl_broker._PERMISSION_STATUS_REFRESHES:
                            break
                    time.sleep(0.01)
                failed = remctl_broker._server_status(runtime)
        self.assertFalse(failed["fullReady"])
        self.assertIn("verification failed", failed["privateProtocol"]["error"])

    def test_fresh_status_is_bounded_and_refreshes_after_short_ttl(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            pending = {**self._permissions(), "automation": "unknown"}
            authorized = {**self._permissions(), "automation": "authorized"}
            first_refresh_started = threading.Event()
            release_first_refresh = threading.Event()
            responses = iter((pending, authorized))

            def refresh(*_args, **_kwargs):
                response = next(responses)
                if response is pending:
                    first_refresh_started.set()
                    self.assertTrue(release_first_refresh.wait(timeout=2))
                return 4242, response, {"compatible": True, "version": 2}

            with (
                mock.patch.object(
                    remctl_broker,
                    "_refresh_verified_status",
                    side_effect=refresh,
                ) as native,
                mock.patch.object(
                    remctl_broker,
                    "_native_status_after_verification",
                    return_value=authorized,
                ),
            ):
                started = time.monotonic()
                first = remctl_broker._permission_status_snapshot(runtime)
                elapsed = time.monotonic() - started
                self.assertTrue(first_refresh_started.is_set())
                release_first_refresh.set()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with remctl_broker._PERMISSION_STATUS_LOCK:
                        if not remctl_broker._PERMISSION_STATUS_REFRESHES:
                            break
                    time.sleep(0.01)
                final = remctl_broker._permission_status_snapshot(runtime)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(first, remctl_broker._unknown_permission_status())
        self.assertEqual(final["automation"], "authorized")
        native.assert_called_once_with(runtime)

    def test_concurrent_status_refreshes_are_single_flight(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            refresh_started = threading.Event()
            release_refresh = threading.Event()
            results = []

            def refresh(*_args, **_kwargs):
                refresh_started.set()
                self.assertTrue(release_refresh.wait(timeout=2))
                return (
                    4242,
                    self._permissions(),
                    {"compatible": True, "version": 2},
                )

            def read_status():
                results.append(remctl_broker._permission_status_snapshot(runtime))

            with mock.patch.object(
                remctl_broker,
                "_refresh_verified_status",
                side_effect=refresh,
            ) as native:
                first = threading.Thread(target=read_status)
                second = threading.Thread(target=read_status)
                first.start()
                self.assertTrue(refresh_started.wait(timeout=1))
                second.start()
                first.join(timeout=1)
                second.join(timeout=1)
                release_refresh.set()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with remctl_broker._PERMISSION_STATUS_LOCK:
                        if not remctl_broker._PERMISSION_STATUS_REFRESHES:
                            break
                    time.sleep(0.01)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(
            results,
            [
                remctl_broker._unknown_permission_status(),
                remctl_broker._unknown_permission_status(),
            ],
        )
        native.assert_called_once_with(
            runtime,
        )

    def test_explicit_result_wins_over_late_background_refresh(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            refresh_started = threading.Event()
            release_refresh = threading.Event()
            stale = {**self._permissions(), "automation": "authorized"}
            explicit = {**self._permissions(), "automation": "denied"}

            def refresh(*_args, **_kwargs):
                refresh_started.set()
                self.assertTrue(release_refresh.wait(timeout=2))
                return 4242, stale, {"compatible": True, "version": 2}

            with mock.patch.object(
                remctl_broker,
                "_refresh_verified_status",
                side_effect=refresh,
            ):
                first = remctl_broker._permission_status_snapshot(runtime)
                self.assertTrue(refresh_started.is_set())
                remctl_broker._remember_permission_status(runtime, explicit)
                release_refresh.set()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with remctl_broker._PERMISSION_STATUS_LOCK:
                        if not remctl_broker._PERMISSION_STATUS_REFRESHES:
                            break
                    time.sleep(0.01)
                final = remctl_broker._permission_status_snapshot(runtime)
        self.assertEqual(first, remctl_broker._unknown_permission_status())
        self.assertEqual(final["automation"], "denied")

    def test_dispatched_status_failures_restart_exact_verified_parent(self):
        for code in (
            "permission_channel_timeout",
            "permission_channel_failed",
            "permission_response_invalid",
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as value:
                runtime = self._runtime(Path(value))
                failure = remctl_broker._ServerError(
                    "native status failed after dispatch",
                    code=code,
                    dispatched=True,
                    indeterminate=True,
                )
                with (
                    mock.patch.object(
                        remctl_broker,
                        "_native_permission_command",
                        side_effect=failure,
                    ) as native,
                    mock.patch.object(
                        remctl_broker,
                        "_terminate_verified_parent_host",
                    ) as terminate,
                    self.assertRaises(remctl_broker._ServerError) as raised,
                ):
                    remctl_broker._native_status_after_verification(runtime, 4242)
                self.assertIs(raised.exception, failure)
                native.assert_called_once_with(
                    runtime,
                    "permissionStatus",
                    timeout=remctl_broker.PERMISSION_STATUS_TIMEOUT_SECONDS,
                    verify_bundle=False,
                )
                terminate.assert_called_once_with(runtime, 4242)
                self.assertTrue(remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.is_set())
                remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.clear()

    def test_failed_native_channel_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as value:
            runtime = self._runtime(Path(value))
            remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.set()
            with (
                mock.patch.object(remctl_broker, "_native_permission_socket") as open_channel,
                self.assertRaises(remctl_broker._ServerError) as raised,
            ):
                remctl_broker._native_permission_command(
                    runtime,
                    "permissionStatus",
                    timeout=0.1,
                    verify_bundle=False,
                )
        self.assertEqual(raised.exception.code, "permission_channel_failed")
        self.assertFalse(raised.exception.dispatched)
        open_channel.assert_not_called()


class PersistentNativePermissionChannelTests(unittest.TestCase):
    def setUp(self):
        remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.clear()

    def tearDown(self):
        remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.clear()

    def test_native_duplicate_is_closed_when_inheritance_guard_fails(self):
        with self._channel():
            duplicate = os.dup(remctl_broker.NATIVE_PERMISSION_FD)
            try:
                with (
                    mock.patch.object(remctl_broker.os, "dup", return_value=duplicate),
                    mock.patch.object(remctl_broker.os, "set_inheritable", side_effect=OSError("guard failed")),
                    self.assertRaises(remctl_broker._ServerError) as error,
                ):
                    remctl_broker._native_permission_socket()
                self.assertEqual(error.exception.code, "permission_channel_unavailable")
                self.assertFalse(error.exception.dispatched)
                self.assertFalse(error.exception.indeterminate)
                with self.assertRaises(OSError):
                    os.fstat(duplicate)
                os.fstat(remctl_broker.NATIVE_PERMISSION_FD)
            finally:
                try:
                    os.close(duplicate)
                except OSError:
                    pass

    @contextlib.contextmanager
    def _channel(self):
        try:
            saved_fd = os.dup(remctl_broker.NATIVE_PERMISSION_FD)
            saved_inheritable = os.get_inheritable(remctl_broker.NATIVE_PERMISSION_FD)
        except OSError:
            saved_fd = None
            saved_inheritable = False
        native, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        child_fd = child.detach()
        try:
            if child_fd != remctl_broker.NATIVE_PERMISSION_FD:
                os.dup2(child_fd, remctl_broker.NATIVE_PERMISSION_FD, inheritable=True)
                os.close(child_fd)
            else:
                os.set_inheritable(child_fd, True)
            with mock.patch.dict(
                os.environ,
                {
                    remctl_broker.ACTIVE_ENV: "1",
                    remctl_broker.NATIVE_FD_ENV: str(remctl_broker.NATIVE_PERMISSION_FD),
                },
            ):
                yield native
        finally:
            native.close()
            try:
                os.close(remctl_broker.NATIVE_PERMISSION_FD)
            except OSError:
                pass
            if saved_fd is not None:
                os.dup2(
                    saved_fd,
                    remctl_broker.NATIVE_PERMISSION_FD,
                    inheritable=saved_inheritable,
                )
                os.close(saved_fd)

    def _runtime(self):
        root = Path("/signed/RemCTL Capability Host.app")
        return remctl_broker.HostedRuntime(
            app=root,
            runtime=root / "Contents/Resources/CapabilityRuntime",
            archive_fd=198,
            cdhash="0" * 40,
            python=Path(sys.executable),
            bridge=root / "bridge",
            private=root / "private",
        )

    def _receive_request(self, native):
        header = self._receive_exact(native, 4)
        size = struct.unpack("!I", header)[0]
        self.assertGreater(size, 0)
        self.assertLessEqual(size, remctl_broker.MAX_NATIVE_REQUEST_BYTES)
        return json.loads(self._receive_exact(native, size))

    def _receive_exact(self, native, size):
        value = bytearray()
        while len(value) < size:
            part = native.recv(size - len(value))
            if not part:
                raise AssertionError("native permission client closed early")
            value.extend(part)
        return bytes(value)

    def _permissions(self, **updates):
        value = {
            "status": "ok",
            "fullDiskAccess": "authorized",
            "reminders": "authorized",
            "automation": "authorized",
            "automationTarget": "com.apple.reminders",
        }
        value.update(updates)
        return value

    def _response(self, **permission_updates):
        return {
            "protocolVersion": remctl_broker.NATIVE_PROTOCOL_VERSION,
            "status": "ok",
            "permissions": self._permissions(**permission_updates),
        }

    def _send_response(self, native, payload):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        native.sendall(struct.pack("!I", len(payload)) + payload)

    def _serve_once(self, native, response, requests):
        requests.append(self._receive_request(native))
        self._send_response(native, response)

    def test_happy_path_uses_exact_frame_and_never_spawns_a_process(self):
        requests = []
        with self._channel() as native:
            server = threading.Thread(
                target=self._serve_once,
                args=(native, self._response(), requests),
            )
            server.start()
            with (
                mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
                mock.patch.object(
                    remctl_broker.subprocess,
                    "run",
                    side_effect=AssertionError("permission IPC must not spawn"),
                ) as run,
            ):
                permissions = remctl_broker._native_permission_command(
                    self._runtime(),
                    "permissionStatus",
                    timeout=1,
                )
            server.join(timeout=1)
        self.assertFalse(server.is_alive())
        self.assertEqual(permissions, self._permissions())
        self.assertEqual(
            requests,
            [{"protocolVersion": 1, "operation": "permissionStatus"}],
        )
        run.assert_not_called()

    def test_native_queue_wait_is_bounded_and_operation_gets_a_fresh_deadline(self):
        class FakeLock:
            def __init__(self):
                self.timeout = None
                self.released = False

            def acquire(self, *, timeout):
                self.timeout = timeout
                return True

            def release(self):
                self.released = True

        lock = FakeLock()
        deadlines = []

        def stop_before_dispatch(_channel, _payload, *, deadline, dispatch_started):
            deadlines.append(deadline)
            self.assertFalse(dispatch_started.is_set())
            raise remctl_broker._ServerError("stop", code="test_stop")

        with (
            mock.patch.object(remctl_broker, "_NATIVE_PERMISSION_LOCK", lock),
            mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
            mock.patch.object(remctl_broker, "_native_permission_socket", return_value=mock.Mock()),
            mock.patch.object(remctl_broker, "_send_native_bytes", side_effect=stop_before_dispatch),
            mock.patch.object(remctl_broker.time, "monotonic", return_value=500.0),
            self.assertRaises(remctl_broker._ServerError) as raised,
        ):
            remctl_broker._native_permission_command(
                self._runtime(),
                "permissionStatus",
                timeout=7,
            )

        self.assertEqual(lock.timeout, remctl_broker.NATIVE_PERMISSION_QUEUE_TIMEOUT_SECONDS)
        self.assertEqual(deadlines, [507.0])
        self.assertTrue(lock.released)
        self.assertEqual(raised.exception.code, "test_stop")
        self.assertFalse(raised.exception.dispatched)

    def test_queued_native_request_times_out_without_dispatch_or_poisoning(self):
        remctl_broker._NATIVE_PERMISSION_LOCK.acquire()
        try:
            with (
                mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
                mock.patch.object(remctl_broker, "_native_permission_socket") as native_socket,
                self.assertRaises(remctl_broker._ServerError) as raised,
            ):
                remctl_broker._native_permission_command(
                    self._runtime(),
                    "permissionStatus",
                    timeout=0.01,
                )
        finally:
            remctl_broker._NATIVE_PERMISSION_LOCK.release()

        self.assertEqual(raised.exception.code, "permission_channel_timeout")
        self.assertFalse(raised.exception.dispatched)
        self.assertFalse(raised.exception.indeterminate)
        self.assertFalse(remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.is_set())
        native_socket.assert_not_called()

    def test_missing_marker_or_fixed_descriptor_fails_closed(self):
        with mock.patch.object(remctl_broker, "_verify_sealed_bundle"):
            with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(
                remctl_broker._ServerError
            ) as missing_marker:
                remctl_broker._native_permission_command(
                    self._runtime(), "permissionStatus", timeout=0.1
                )
            with mock.patch.dict(
                os.environ,
                {
                    remctl_broker.ACTIVE_ENV: "1",
                    remctl_broker.NATIVE_FD_ENV: "198",
                },
                clear=True,
            ), self.assertRaises(remctl_broker._ServerError) as wrong_fd:
                remctl_broker._native_permission_command(
                    self._runtime(), "permissionStatus", timeout=0.1
                )
        self.assertEqual(missing_marker.exception.code, "permission_channel_unavailable")
        self.assertEqual(wrong_fd.exception.code, "permission_channel_unavailable")

    def test_malformed_frames_fail_closed_and_poison_the_channel(self):
        frames = (
            struct.pack("!I", 0),
            struct.pack("!I", remctl_broker.MAX_NATIVE_RESPONSE_BYTES + 1),
            struct.pack("!I", 10) + b"{}",
        )
        for frame in frames:
            with self.subTest(frame=frame[:4]):
                remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.clear()
                with self._channel() as native:
                    def respond():
                        self._receive_request(native)
                        native.sendall(frame)
                        native.shutdown(socket.SHUT_WR)

                    server = threading.Thread(target=respond)
                    server.start()
                    with (
                        mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
                        self.assertRaises(remctl_broker._ServerError) as raised,
                    ):
                        remctl_broker._native_permission_command(
                            self._runtime(), "permissionStatus", timeout=1
                        )
                    server.join(timeout=1)
                self.assertFalse(server.is_alive())
                self.assertIn(
                    raised.exception.code,
                    {"permission_response_invalid", "permission_channel_failed"},
                )
                self.assertTrue(remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.is_set())

    def test_response_schema_and_duplicate_keys_are_rejected(self):
        malformed = (
            {"protocolVersion": 2, "status": "ok", "permissions": self._permissions()},
            {"protocolVersion": True, "status": "ok", "permissions": self._permissions()},
            {"protocolVersion": 1.0, "status": "ok", "permissions": self._permissions()},
            {
                "protocolVersion": 1,
                "status": "ok",
                "permissions": self._permissions(),
                "extra": True,
            },
            {
                "protocolVersion": 1,
                "status": "ok",
                "permissions": {**self._permissions(), "automationTarget": "com.apple.finder"},
            },
            b'{"protocolVersion":1,"protocolVersion":1,"status":"ok","permissions":{}}',
        )
        for response in malformed:
            with self.subTest(response=response):
                remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.clear()
                with self._channel() as native:
                    server = threading.Thread(
                        target=self._serve_once,
                        args=(native, response, []),
                    )
                    server.start()
                    with (
                        mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
                        self.assertRaises(remctl_broker._ServerError) as raised,
                    ):
                        remctl_broker._native_permission_command(
                            self._runtime(), "permissionStatus", timeout=1
                        )
                    server.join(timeout=1)
                self.assertFalse(server.is_alive())
                self.assertEqual(raised.exception.code, "permission_response_invalid")

    def test_queued_bytes_after_one_native_response_are_rejected(self):
        remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.clear()
        with self._channel() as native:
            def respond_with_trailing_byte():
                self._receive_request(native)
                payload = json.dumps(self._response(), separators=(",", ":")).encode()
                native.sendall(struct.pack("!I", len(payload)) + payload + b"x")

            server = threading.Thread(target=respond_with_trailing_byte)
            server.start()
            with (
                mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
                self.assertRaises(remctl_broker._ServerError) as raised,
            ):
                remctl_broker._native_permission_command(
                    self._runtime(), "permissionStatus", timeout=1
                )
            server.join(timeout=1)
        self.assertFalse(server.is_alive())
        self.assertEqual(raised.exception.code, "permission_response_invalid")
        self.assertTrue(remctl_broker._NATIVE_PERMISSION_CHANNEL_FAILED.is_set())

    def test_deadline_is_absolute_across_a_slow_response(self):
        with self._channel() as native:
            def respond_slowly():
                self._receive_request(native)
                payload = json.dumps(self._response(), separators=(",", ":")).encode()
                frame = struct.pack("!I", len(payload)) + payload
                for byte in frame:
                    try:
                        native.send(bytes([byte]))
                    except OSError:
                        return
                    time.sleep(0.02)

            server = threading.Thread(target=respond_slowly)
            server.start()
            started = time.monotonic()
            with (
                mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
                self.assertRaises(remctl_broker._ServerError) as raised,
            ):
                remctl_broker._native_permission_command(
                    self._runtime(), "permissionStatus", timeout=0.08
                )
            elapsed = time.monotonic() - started
        server.join(timeout=1)
        self.assertFalse(server.is_alive())
        self.assertEqual(raised.exception.code, "permission_channel_timeout")
        self.assertLess(elapsed, 0.4)

    def test_concurrent_requests_are_serialized_on_the_persistent_stream(self):
        first_received = threading.Event()
        allow_first_response = threading.Event()
        observations = []
        with self._channel() as native:
            def respond_twice():
                observations.append(self._receive_request(native))
                first_received.set()
                self.assertTrue(allow_first_response.wait(timeout=1))
                readable, _, _ = select.select([native], [], [], 0)
                observations.append("pipelined" if readable else "serialized")
                self._send_response(native, self._response())
                observations.append(self._receive_request(native))
                self._send_response(native, self._response())

            server = threading.Thread(target=respond_twice)
            server.start()
            errors = []

            def call(operation):
                try:
                    remctl_broker._native_permission_command(
                        self._runtime(), operation, timeout=1
                    )
                except Exception as exc:
                    errors.append(exc)

            with mock.patch.object(remctl_broker, "_verify_sealed_bundle"):
                first = threading.Thread(target=call, args=("permissionStatus",))
                first.start()
                self.assertTrue(first_received.wait(timeout=1))
                second = threading.Thread(target=call, args=("requestReminders",))
                second.start()
                time.sleep(0.05)
                allow_first_response.set()
                first.join(timeout=1)
                second.join(timeout=1)
            server.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(server.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observations[1], "serialized")
        self.assertEqual(
            [observations[0]["operation"], observations[2]["operation"]],
            ["permissionStatus", "requestReminders"],
        )

    def test_native_channel_is_not_in_cli_environment_or_pass_fds(self):
        class Process:
            pid = 424242
            returncode = 0

            def poll(self):
                return 0

        with (
            mock.patch.object(remctl_broker, "_verify_sealed_bundle"),
            mock.patch.object(remctl_broker, "_load_real_parser", return_value=object()),
            mock.patch.object(
                remctl_broker,
                "validate_argv",
                return_value=(["today"], object()),
            ),
            mock.patch.object(
                remctl_broker,
                "materialize_inputs",
                return_value=(["today"], None, None, None),
            ),
            mock.patch.object(
                remctl_broker,
                "_collect_bounded_output",
                return_value=(0, b"", b"", False),
            ),
            mock.patch.object(
                remctl_broker.subprocess,
                "Popen",
                return_value=Process(),
            ) as popen,
        ):
            remctl_broker._run_cli(
                ["today"],
                runtime=self._runtime(),
                capabilities={},
                stdin_bytes=b"",
                timeout=1,
            )
        invocation = popen.call_args.kwargs
        self.assertNotIn(remctl_broker.NATIVE_FD_ENV, invocation["env"])
        self.assertNotIn(remctl_broker.NATIVE_PERMISSION_FD, invocation["pass_fds"])


if __name__ == "__main__":
    unittest.main()
