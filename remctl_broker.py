"""Client and server transport for the signed RemCTL Capability Host.

The broker is intentionally not a general command runner. It accepts only argv
that the real RemCTL parser classifies as protected, plus descriptor-backed
resources planned by the unprivileged caller.
"""

from __future__ import annotations

import array
import base64
import concurrent.futures
import ctypes
import importlib
import importlib.machinery
import importlib.util
import json
import os
import plistlib
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from remctl_capabilities import (
    CapabilityBundle,
    CapabilityError,
    DescriptorCapability,
    MAX_CAPABILITIES,
    MAX_STDIN_BYTES,
    materialize_inputs,
    plan_invocation,
    validate_capability_bindings,
    validate_received_capabilities,
)
from remctl_capability_policy import (
    CapabilityPolicyError,
    PROTOCOL_VERSION,
    SCOPE,
    command_scope,
    request_schema,
    validate_argv,
)
from remctl_runtime import capability_host_mode


APP_NAME = "RemCTL Capability Host.app"
BUNDLE_IDENTIFIER = "net.macstories.remctl.capability-host"
EXECUTABLE_NAME = "RemCTL Capability Host"
LAUNCH_AGENT_LABEL = "net.macstories.remctl.capability-host"

MODE_ENV = "REMCTL_CAPABILITY_HOST"
ACTIVE_ENV = "REMCTL_CAPABILITY_HOST_ACTIVE"
APP_ENV = "REMCTL_CAPABILITY_HOST_APP"
SOCKET_ENV = "REMCTL_CAPABILITY_HOST_SOCKET"
AGENT_ENV = "REMCTL_CAPABILITY_HOST_LAUNCH_AGENT"
ARCHIVE_FD_ENV = "REMCTL_CAPABILITY_ARCHIVE_FD"
NATIVE_FD_ENV = "REMCTL_CAPABILITY_NATIVE_FD"
HOST_CDHASH_ENV = "REMCTL_CAPABILITY_HOST_CDHASH"
RUNTIME_ENV = "REMCTL_CAPABILITY_RUNTIME"

MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_RESPONSE_BYTES = 192 * 1024 * 1024
MAX_STDOUT_BYTES = 64 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024 * 1024
MAX_COMBINED_OUTPUT_BYTES = 96 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 900.0
SOCKET_TIMEOUT_SECONDS = 5.0
SIGNAL_CONTROL_MAX_BYTES = 256
SIGNAL_CONTROL_PARTIAL_TIMEOUT_SECONDS = 0.5
SIGNAL_CONTROL_GRACE_SECONDS = 2.0
CLIENT_SIGNAL_WAIT_SECONDS = 5.0
MAX_CLIENT_WORKERS = 16
MAX_ANCILLARY_FDS = 256
PRIVATE_PROTOCOL_VERSION = 2
NATIVE_PROTOCOL_VERSION = 1
NATIVE_PERMISSION_FD = 199
MAX_NATIVE_REQUEST_BYTES = 4096
MAX_NATIVE_RESPONSE_BYTES = 65536
PERMISSION_STATUS_TIMEOUT_SECONDS = 2.0
PERMISSION_STATUS_TTL_SECONDS = 5.0
PERMISSION_STATUS_INITIAL_WAIT_SECONDS = 0.25
PERMISSION_STATUS_HYDRATION_SECONDS = 20.0
PERMISSION_STATUS_FAILURE_BACKOFF_SECONDS = 10.0
NATIVE_PERMISSION_QUEUE_TIMEOUT_SECONDS = 2.0
_CDHASH_PATTERN = re.compile(r"(?m)^CDHash=([0-9a-fA-F]{40})$")
_NATIVE_ERROR_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")

_DISPATCH_LOCK = threading.Lock()
_NATIVE_PERMISSION_LOCK = threading.Lock()
_NATIVE_PERMISSION_CHANNEL_FAILED = threading.Event()
_PERMISSION_STATUS_LOCK = threading.Lock()
_PERMISSION_STATUS_CACHE: dict[
    tuple[str, str], tuple[float, dict[str, Any]]
] = {}
_PERMISSION_STATUS_GENERATION: dict[tuple[str, str], int] = {}
_PERMISSION_STATUS_REFRESHES: dict[tuple[str, str], threading.Event] = {}
_PRIVATE_PROTOCOL_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_PERMISSION_STATUS_FAILURES: dict[tuple[str, str], tuple[float, str]] = {}
_CLIENT_SLOTS = threading.BoundedSemaphore(MAX_CLIENT_WORKERS)
_SERVICE_STOPPING = threading.Event()
_ACTIVE_PROCESSES: set[subprocess.Popen[bytes]] = set()
_ACTIVE_PROCESSES_LOCK = threading.Lock()
CLIENT_ROOT = Path(__file__).resolve().parent


class CapabilityHostError(RuntimeError):
    """Base client-visible host error with retry-safety metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "capability_host_error",
        dispatched: bool = False,
        indeterminate: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.dispatched = dispatched
        self.indeterminate = indeterminate

    @property
    def retry_safe(self) -> bool:
        return not self.dispatched and not self.indeterminate


class HostUnavailable(CapabilityHostError):
    def __init__(self, message: str = "RemCTL Capability Host is unavailable") -> None:
        super().__init__(message, code="capability_host_unavailable")


class HostUnhealthy(CapabilityHostError):
    def __init__(self, message: str, *, code: str = "capability_host_unhealthy") -> None:
        super().__init__(message, code=code)


class HostRejected(CapabilityHostError):
    pass


class HostIndeterminate(CapabilityHostError):
    def __init__(self, message: str, *, code: str = "capability_host_indeterminate") -> None:
        super().__init__(message, code=code, dispatched=True, indeterminate=True)


class _ServerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        dispatched: bool = False,
        indeterminate: bool = False,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stderr_relayed: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.dispatched = dispatched
        self.indeterminate = indeterminate
        self.stdout = stdout
        self.stderr = stderr
        self.stderr_relayed = stderr_relayed


class _NativePermissionError(_ServerError):
    """A well-formed error returned by the persistent native host."""


class _ForwardedClientSignal(BaseException):
    """Interrupt a blocking receive after forwarding a caller signal."""


@dataclass(frozen=True)
class HostedRuntime:
    app: Path
    runtime: Path
    archive_fd: int
    cdhash: str
    python: Path
    bridge: Path
    private: Path


def _installed_path_marker(path: Path, *, expected_name: str | None = None) -> Path | None:
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) & 0o022
            or path.is_symlink()
            or details.st_size > 4096
        ):
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if len(lines) != 1 or not lines[0] or "\x00" in lines[0]:
        return None
    value = Path(lines[0])
    if (
        not value.is_absolute()
        or value != value.resolve(strict=False)
        or (expected_name is not None and value.name != expected_name)
    ):
        return None
    return value


def app_path() -> Path:
    override = os.environ.get(APP_ENV)
    if override:
        return Path(override).expanduser()
    installed = _installed_path_marker(
        CLIENT_ROOT / ".remctl-capability-host-app",
        expected_name=APP_NAME,
    )
    return installed or Path.home() / "Applications" / APP_NAME


def executable_path() -> Path:
    return app_path() / "Contents/MacOS" / EXECUTABLE_NAME


def socket_path() -> Path:
    override = os.environ.get(SOCKET_ENV)
    if override:
        return Path(override).expanduser()
    installed = _installed_path_marker(
        app_path() / "Contents/Resources/remctl-capability-host-socket-path"
    )
    return installed or Path.home() / "Library/Application Support/RemCTL/capability-host.sock"


def launch_agent_path() -> Path:
    override = os.environ.get(AGENT_ENV)
    if override:
        return Path(override).expanduser()
    installed_agent = _installed_path_marker(
        app_path()
        / "Contents/Resources/remctl-capability-host-launch-agent-path",
        expected_name=f"{LAUNCH_AGENT_LABEL}.plist",
    )
    if installed_agent is not None:
        return installed_agent
    # launchd loads per-user agents at login only from ~/Library/LaunchAgents,
    # whatever prefix the app was installed under.
    return Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def mode() -> str:
    return capability_host_mode()


def _app_installed() -> bool:
    executable = executable_path()
    return executable.is_file() and os.access(executable, os.X_OK)


def _socket_metadata(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "isSocket": False,
        "ownerUid": None,
        "mode": None,
        "secure": False,
        "error": None,
    }
    try:
        canonical_parent = path.parent.resolve(strict=False)
    except (OSError, RuntimeError):
        canonical_parent = None
    if not path.is_absolute() or path.parent != canonical_parent:
        result["error"] = "socket parent path must be absolute and canonical"
        return result
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        result["error"] = f"unsafe socket parent: {exc}"
        return result
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        result["error"] = "socket parent must be an owner-only directory"
        return result
    try:
        details = path.lstat()
    except FileNotFoundError:
        return result
    except OSError as exc:
        result["error"] = str(exc)
        return result
    result.update(
        {
            "exists": True,
            "isSocket": stat.S_ISSOCK(details.st_mode),
            "ownerUid": details.st_uid,
            "mode": f"{stat.S_IMODE(details.st_mode):04o}",
        }
    )
    result["secure"] = bool(
        result["isSocket"]
        and details.st_uid == os.getuid()
        and stat.S_IMODE(details.st_mode) == 0o600
    )
    if not result["secure"]:
        result["error"] = "socket must be an owner-only Unix socket"
    return result


def should_route(parsed_args: Any) -> bool:
    try:
        hosted = command_scope(getattr(parsed_args, "cmd", None)) == "hosted"
    except CapabilityPolicyError:
        raise
    if not hosted:
        return False
    requested = mode()
    if requested == "direct":
        return False
    if requested == "force":
        return True
    return _socket_metadata(socket_path()).get("secure") is True or _app_installed()


def _json_bytes(payload: dict[str, Any], *, limit: int) -> bytes:
    try:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _ServerError("IPC payload is not JSON serializable", code="invalid_payload") from exc
    if len(data) > limit:
        raise _ServerError("IPC payload exceeds its size limit", code="payload_too_large")
    return data


def _send_frame(
    connection: socket.socket,
    payload: dict[str, Any],
    *,
    limit: int,
    descriptors: list[int] | None = None,
) -> None:
    body = _json_bytes(payload, limit=limit)
    framed = struct.pack("!I", len(body)) + body
    if not descriptors:
        connection.sendall(framed)
        return
    rights = array.array("i", descriptors)
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)]
    sent = connection.sendmsg([framed], ancillary)
    if sent <= 0:
        raise OSError("failed to send capability request")
    if sent < len(framed):
        connection.sendall(framed[sent:])


def _recv_exact(
    connection: socket.socket,
    size: int,
    *,
    deadline: float | None = None,
) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("IPC receive deadline expired")
            connection.settimeout(remaining)
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise EOFError("peer closed the capability connection")
        chunks.extend(chunk)
    return bytes(chunks)


def _decode_body(
    header: bytes,
    connection: socket.socket,
    *,
    limit: int,
    deadline: float | None = None,
) -> dict[str, Any]:
    if len(header) != 4:
        raise _ServerError("invalid IPC frame header", code="invalid_frame")
    (length,) = struct.unpack("!I", header)
    if length <= 0 or length > limit:
        raise _ServerError("IPC frame length is invalid", code="invalid_frame")
    try:
        payload = json.loads(_recv_exact(connection, length, deadline=deadline))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ServerError("IPC frame is not valid JSON", code="invalid_frame") from exc
    if not isinstance(payload, dict):
        raise _ServerError("IPC payload must be an object", code="invalid_frame")
    return payload


def _recv_response(connection: socket.socket, *, deadline: float) -> dict[str, Any]:
    return _decode_body(
        _recv_exact(connection, 4, deadline=deadline),
        connection,
        limit=MAX_RESPONSE_BYTES,
        deadline=deadline,
    )


def _recv_request(connection: socket.socket) -> tuple[dict[str, Any], list[int]]:
    deadline = time.monotonic() + SOCKET_TIMEOUT_SECONDS
    ancillary_size = socket.CMSG_SPACE(MAX_ANCILLARY_FDS * array.array("i").itemsize)
    descriptors: list[int] = []
    try:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        header, ancillary, flags, _address = connection.recvmsg(4, ancillary_size)
        if not header:
            raise EOFError("peer closed before sending a request")
        control_error: str | None = None
        for level, kind, data in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                control_error = "unexpected descriptor control message"
                continue
            rights = array.array("i")
            usable = len(data) - (len(data) % rights.itemsize)
            rights.frombytes(data[:usable])
            descriptors.extend(rights.tolist())
            if usable != len(data):
                control_error = "descriptor control message is malformed"
        if flags & getattr(socket, "MSG_CTRUNC", 0):
            raise _ServerError(
                "descriptor control message was truncated",
                code="invalid_capabilities",
            )
        if control_error is not None:
            raise _ServerError(control_error, code="invalid_capabilities")
        if len(descriptors) > MAX_CAPABILITIES:
            raise _ServerError("too many received descriptors", code="invalid_capabilities")
        if len(header) < 4:
            header += _recv_exact(connection, 4 - len(header), deadline=deadline)
        return (
            _decode_body(
                header,
                connection,
                limit=MAX_REQUEST_BYTES,
                deadline=deadline,
            ),
            descriptors,
        )
    except Exception:
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _request(
    payload: dict[str, Any],
    *,
    timeout: float,
    descriptors: list[int] | None = None,
    forwarded_signals: list[int] | None = None,
) -> dict[str, Any]:
    metadata = _socket_metadata(socket_path())
    if metadata.get("secure") is not True:
        if metadata.get("exists"):
            raise HostUnhealthy(metadata.get("error") or "Capability Host socket is unsafe")
        raise HostUnavailable()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    send_attempted = False
    request_sent = False
    transmitting_request = False
    deadline = time.monotonic() + timeout
    original_handlers: dict[int, Any] = {}

    def forward_signal(signal_number: int, _frame: Any) -> None:
        if forwarded_signals:
            return
        if forwarded_signals is not None:
            forwarded_signals.append(signal_number)
            if request_sent:
                try:
                    _send_run_signal_control(connection, signal_number)
                except (OSError, _ServerError):
                    pass
        # Never interrupt or write into a request frame while it is being sent.
        # The pending signal is forwarded immediately after the complete frame.
        if transmitting_request:
            return
        raise _ForwardedClientSignal()

    try:
        if (
            payload.get("operation") == "run"
            and forwarded_signals is not None
            and threading.current_thread() is threading.main_thread()
        ):
            for signal_number in (int(signal.SIGINT), int(signal.SIGTERM)):
                original_handlers[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, forward_signal)
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.connect(str(socket_path()))
        send_attempted = True
        transmitting_request = payload.get("operation") == "run"
        _send_frame(
            connection,
            payload,
            limit=MAX_REQUEST_BYTES,
            descriptors=descriptors,
        )
        request_sent = True
        transmitting_request = False
        if forwarded_signals:
            _send_run_signal_control(connection, forwarded_signals[0])
        receive_deadline = deadline
        if forwarded_signals:
            receive_deadline = min(
                receive_deadline,
                time.monotonic() + CLIENT_SIGNAL_WAIT_SECONDS,
            )
        while True:
            try:
                response = _recv_response(connection, deadline=receive_deadline)
                break
            except _ForwardedClientSignal:
                receive_deadline = min(
                    receive_deadline,
                    time.monotonic() + CLIENT_SIGNAL_WAIT_SECONDS,
                )
    except (HostUnavailable, HostUnhealthy):
        raise
    except (OSError, EOFError, _ServerError) as exc:
        if send_attempted and payload.get("operation") == "run":
            raise HostIndeterminate(
                "Capability Host connection ended after the request was sent"
            ) from exc
        raise HostUnavailable(f"RemCTL Capability Host request failed: {exc}") from exc
    finally:
        for signal_number, handler in original_handlers.items():
            try:
                signal.signal(signal_number, handler)
            except (OSError, RuntimeError, ValueError):
                pass
        connection.close()
    return response


def _write_stream_bytes(stream: Any, payload: bytes) -> None:
    if not payload:
        return
    target = getattr(stream, "buffer", stream)
    try:
        target.write(payload)
    except TypeError:
        stream.write(payload.decode("utf-8", errors="surrogateescape"))
    try:
        target.flush()
    except (AttributeError, OSError):
        pass


def _output_streams_share_sink() -> bool:
    """Return true only when stdout and stderr resolve to the same open sink."""

    try:
        stdout_details = os.fstat(sys.stdout.fileno())
        stderr_details = os.fstat(sys.stderr.fileno())
        return bool(os.path.samestat(stdout_details, stderr_details))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _send_run_signal_control(connection: socket.socket, signal_number: int) -> None:
    names = {
        int(signal.SIGINT): "SIGINT",
        int(signal.SIGTERM): "SIGTERM",
    }
    try:
        signal_name = names[signal_number]
    except KeyError as exc:
        raise _ServerError("unsupported run signal", code="invalid_signal_control") from exc
    payload = json.dumps(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "operation": "signal",
            "signal": signal_name,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if not payload or len(payload) > SIGNAL_CONTROL_MAX_BYTES:
        raise _ServerError("run signal control is too large", code="invalid_signal_control")
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def _completion_signal(exit_code: Any) -> int | None:
    """Return a terminating signal number, or None for a normal exit status."""

    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise HostIndeterminate("Capability Host returned an invalid exit status")
    if 0 <= exit_code <= 255:
        return None
    if exit_code >= 0:
        raise HostIndeterminate("Capability Host returned an invalid exit status")
    signal_number = -exit_code
    try:
        valid_signals = {int(value) for value in signal.valid_signals()}
    except AttributeError:
        valid_signals = set(range(1, signal.NSIG))
    nonterminating_names = (
        "SIGCHLD",
        "SIGCONT",
        "SIGINFO",
        "SIGSTOP",
        "SIGTSTP",
        "SIGTTIN",
        "SIGTTOU",
        "SIGURG",
        "SIGWINCH",
    )
    nonterminating = {
        int(value)
        for name in nonterminating_names
        if (value := getattr(signal, name, None)) is not None
    }
    if signal_number not in valid_signals or signal_number in nonterminating:
        raise HostIndeterminate("Capability Host returned an invalid exit signal")
    return signal_number


def exit_with_status(exit_code: int) -> None:
    """Exit normally or reproduce a hosted child's terminating signal."""

    signal_number = _completion_signal(exit_code)
    if signal_number is None:
        if exit_code:
            raise SystemExit(exit_code)
        return
    if signal_number != int(signal.SIGKILL):
        try:
            signal.signal(signal_number, signal.SIG_DFL)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HostIndeterminate(
                "RemCTL could not restore the hosted termination signal",
                code="signal_reproduction_failed",
            ) from exc
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if pthread_sigmask is not None:
        try:
            pthread_sigmask(signal.SIG_UNBLOCK, {signal_number})
        except (OSError, ValueError) as exc:
            if signal_number != int(signal.SIGKILL):
                raise HostIndeterminate(
                    "RemCTL could not unblock the hosted termination signal",
                    code="signal_reproduction_failed",
                ) from exc
    os.kill(os.getpid(), signal_number)
    os._exit(128 + signal_number)


def _load_real_parser() -> Any:
    try:
        module = importlib.import_module("remctl_cli")
    except ImportError:
        if os.environ.get(ACTIVE_ENV) == "1":
            raise _ServerError(
                "sealed RemCTL parser module is unavailable",
                code="host_runtime_invalid",
            )
        source = Path(__file__).resolve().with_name("remctl")
        loader = importlib.machinery.SourceFileLoader("_remctl_source_cli", str(source))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        if spec is None:
            raise RuntimeError("could not load the RemCTL parser")
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
    parser = module.build_parser()
    return parser[0] if isinstance(parser, tuple) else parser


def dispatch(
    argv: list[str],
    *,
    parsed_args: Any | None = None,
    timeout: float | None = None,
) -> int:
    """Execute one protected command and reproduce its exact byte streams."""

    command_timeout = timeout or COMMAND_TIMEOUT_SECONDS + 5
    try:
        if parsed_args is None:
            _validated, parsed_args = validate_argv(argv, parser=_load_real_parser())
        capabilities = plan_invocation(argv, parsed_args)
    except (CapabilityError, CapabilityPolicyError, OSError) as exc:
        raise HostRejected(
            str(exc),
            code="invalid_local_capability_request",
        ) from exc
    forwarded_signals: list[int] = []
    try:
        with capabilities:
            has_prompt_capability = any(
                isinstance(item, dict)
                and item.get("kind") == "tty"
                and item.get("purpose") in {"stdin", "stderr"}
                for item in capabilities.metadata
            )
            request = {
                "protocolVersion": PROTOCOL_VERSION,
                "operation": "run",
                "argv": capabilities.argv,
                "capabilities": capabilities.metadata,
                "stdinBase64": capabilities.stdin_base64,
                "mergeOutput": bool(
                    not has_prompt_capability and _output_streams_share_sink()
                ),
                "deadlineEpoch": time.time() + command_timeout,
            }
            response = _request(
                request,
                timeout=command_timeout,
                descriptors=capabilities.fds,
                forwarded_signals=forwarded_signals,
            )
        return _handle_run_response(response)
    finally:
        if forwarded_signals:
            exit_with_status(-forwarded_signals[0])


def _handle_run_response(response: dict[str, Any]) -> int:
    """Validate and replay one exact run response."""

    if (
        type(response.get("protocolVersion")) is not int
        or response.get("protocolVersion") != PROTOCOL_VERSION
    ):
        raise HostIndeterminate(
            "Capability Host returned a stale protocol after the run request",
            code="protocol_mismatch",
        )
    status_value = response.get("status")
    if status_value == "error":
        expected_error_keys = {
            "status",
            "protocolVersion",
            "code",
            "message",
            "dispatched",
            "indeterminate",
        }
        output_error_keys = expected_error_keys | {
            "stdoutBase64",
            "stderrBase64",
            "stderrRelayed",
        }
        code = response.get("code")
        message = response.get("message")
        dispatched = response.get("dispatched")
        indeterminate = response.get("indeterminate")
        if (
            frozenset(response) not in {frozenset(expected_error_keys), frozenset(output_error_keys)}
            or not isinstance(code, str)
            or not code
            or not isinstance(message, str)
            or not message
            or not isinstance(dispatched, bool)
            or not isinstance(indeterminate, bool)
        ):
            raise HostIndeterminate(
                "Capability Host returned a malformed run error",
                code="malformed_run_response",
            )
        if set(response) == output_error_keys:
            try:
                stdout = base64.b64decode(response["stdoutBase64"], validate=True)
                stderr = base64.b64decode(response["stderrBase64"], validate=True)
            except (ValueError, TypeError) as exc:
                raise HostIndeterminate(
                    "Capability Host returned malformed partial output",
                    code="malformed_run_response",
                ) from exc
            if (
                not isinstance(response["stderrRelayed"], bool)
                or len(stdout) > MAX_STDOUT_BYTES
                or len(stderr) > MAX_STDERR_BYTES
                or len(stdout) + len(stderr) > MAX_COMBINED_OUTPUT_BYTES
            ):
                raise HostIndeterminate(
                    "Capability Host returned partial output beyond its limits",
                    code="malformed_run_response",
                )
            _write_stream_bytes(sys.stdout, stdout)
            if response["stderrRelayed"] is not True:
                _write_stream_bytes(sys.stderr, stderr)
        if dispatched or indeterminate:
            raise HostIndeterminate(message, code=code)
        raise HostRejected(message, code=code)
    expected_completion_keys = {
        "status",
        "protocolVersion",
        "dispatched",
        "indeterminate",
        "exitCode",
        "stdoutBase64",
        "stderrBase64",
        "stderrRelayed",
    }
    if (
        status_value != "completed"
        or set(response) != expected_completion_keys
        or response.get("dispatched") is not True
        or response.get("indeterminate") is not False
        or not isinstance(response.get("stderrRelayed"), bool)
    ):
        raise HostIndeterminate(
            "Capability Host returned a malformed completion",
            code="malformed_run_response",
        )
    try:
        stdout = base64.b64decode(response.get("stdoutBase64", ""), validate=True)
        stderr = base64.b64decode(response.get("stderrBase64", ""), validate=True)
        exit_code = response["exitCode"]
    except (ValueError, KeyError, TypeError) as exc:
        raise HostIndeterminate("Capability Host returned a malformed completion") from exc
    if (
        len(stdout) > MAX_STDOUT_BYTES
        or len(stderr) > MAX_STDERR_BYTES
        or len(stdout) + len(stderr) > MAX_COMBINED_OUTPUT_BYTES
    ):
        raise HostIndeterminate("Capability Host returned output beyond its limits")
    _completion_signal(exit_code)
    _write_stream_bytes(sys.stdout, stdout)
    if response.get("stderrRelayed") is not True:
        _write_stream_bytes(sys.stderr, stderr)
    return exit_code


def _launch_agent_status() -> dict[str, Any]:
    path = launch_agent_path()
    installed = path.is_file()
    loaded = False
    if installed:
        try:
            process = subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            loaded = process.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"path": str(path), "installed": installed, "loaded": loaded}


def _app_status() -> dict[str, Any]:
    app = app_path()
    executable = executable_path()
    identifier: str | None = None
    try:
        with (app / "Contents/Info.plist").open("rb") as handle:
            value = plistlib.load(handle).get("CFBundleIdentifier")
            identifier = value if isinstance(value, str) else None
    except (OSError, plistlib.InvalidFileException):
        pass
    signature = _signature_status(app) if executable.is_file() else _empty_signature_status()
    return {
        "path": str(app),
        "installed": executable.is_file() and os.access(executable, os.X_OK),
        "executable": str(executable),
        "bundleIdentifier": identifier,
        "signature": signature,
    }


def _empty_signature_status() -> dict[str, Any]:
    return {
        "valid": False,
        "identifier": None,
        "teamID": None,
        "cdhash": None,
        "designatedRequirement": None,
        "error": "Capability Host is not installed",
    }


def _signature_status(app: Path) -> dict[str, Any]:
    result = _empty_signature_status()
    try:
        verified = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        described = subprocess.run(
            ["/usr/bin/codesign", "-dvvv", "-r-", str(app)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
        return result
    description = (described.stdout + described.stderr).decode("utf-8", errors="replace")

    def field(name: str) -> str | None:
        match = re.search(rf"(?m)^{re.escape(name)}=(.+)$", description)
        return match.group(1).strip() if match else None

    requirement_match = re.search(r"(?m)^(?:# )?designated => (.+)$", description)
    result.update(
        {
            "valid": verified.returncode == 0 and described.returncode == 0,
            "identifier": field("Identifier"),
            "teamID": field("TeamIdentifier"),
            "cdhash": field("CDHash"),
            "designatedRequirement": (
                requirement_match.group(1).strip() if requirement_match else None
            ),
            "error": None,
        }
    )
    if not result["valid"]:
        message = verified.stderr or described.stderr
        result["error"] = message.decode("utf-8", errors="replace").strip()[:2000]
    return result


def _base_status() -> dict[str, Any]:
    return {
        "status": "error",
        "installed": _app_installed(),
        "available": False,
        "ready": False,
        "fullReady": False,
        "protocolVersion": None,
        "expectedProtocolVersion": PROTOCOL_VERSION,
        "scope": SCOPE,
        "supportsStdin": True,
        "supportsTTYStdin": True,
        "supportsWorkingDirectory": False,
        "socket": _socket_metadata(socket_path()),
        "launchAgent": _launch_agent_status(),
        "app": _app_status(),
        "permissions": None,
        "privateProtocol": None,
        "commandScope": None,
        "error": None,
    }


def status(*, timeout: float = 10) -> dict[str, Any]:
    """Return a total local/remote diagnostic without raising."""

    result = _base_status()
    if result["socket"].get("secure") is not True:
        result["error"] = result["socket"].get("error") or "Capability Host is not running"
        return result
    try:
        response = _request(
            {"protocolVersion": PROTOCOL_VERSION, "operation": "status"},
            timeout=timeout,
        )
    except CapabilityHostError as exc:
        result["error"] = str(exc)
        return result
    if response.get("protocolVersion") != PROTOCOL_VERSION:
        result["error"] = "Capability Host protocol mismatch"
        result["protocolVersion"] = response.get("protocolVersion")
        return result
    for key in (
        "status",
        "ready",
        "fullReady",
        "protocolVersion",
        "scope",
        "supportsStdin",
        "supportsTTYStdin",
        "supportsWorkingDirectory",
        "permissions",
        "privateProtocol",
        "commandScope",
        "error",
    ):
        if key in response:
            result[key] = response[key]
    result["available"] = True
    result["installed"] = True
    return result


def ping(*, timeout: float = 10) -> dict[str, Any]:
    """Verify that the signed host can accept the private transport protocol."""

    response = _request(
        {"protocolVersion": PROTOCOL_VERSION, "operation": "ping"},
        timeout=timeout,
    )
    expected_keys = {"status", "protocolVersion"}
    if (
        set(response) != expected_keys
        or response.get("status") != "ok"
        or response.get("protocolVersion") != PROTOCOL_VERSION
    ):
        raise HostUnhealthy("Capability Host returned an invalid ping response")
    return response


def permission_status(*, timeout: float = 10) -> dict[str, Any]:
    response = _request(
        {"protocolVersion": PROTOCOL_VERSION, "operation": "permissionStatus"},
        timeout=timeout,
    )
    if response.get("status") != "ok" or not isinstance(response.get("permissions"), dict):
        raise HostUnhealthy(str(response.get("message") or "invalid permission status"))
    return response["permissions"]


def request_permission(kind: str, *, timeout: float = 305) -> dict[str, Any]:
    if kind not in {"reminders", "automation"}:
        raise ValueError(f"unsupported Capability Host permission: {kind}")
    response = _request(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "operation": "requestPermission",
            "permission": kind,
        },
        timeout=timeout,
    )
    if response.get("status") != "ok" or not isinstance(response.get("permissions"), dict):
        raise HostUnhealthy(str(response.get("message") or "permission request failed"))
    permissions = response["permissions"]
    terminal_outcome = permissions.get(kind)
    terminal_errors = {
        "cancelled": (
            "permission_prompt_cancelled",
            f"Capability Host {kind} permission request was cancelled",
        ),
        "timedOut": (
            "permission_prompt_timed_out",
            f"Capability Host {kind} permission request timed out",
        ),
        "promptUnavailable": (
            "permission_prompt_unavailable",
            f"Capability Host could not present the {kind} permission prompt",
        ),
    }
    if terminal_outcome in terminal_errors:
        code, message = terminal_errors[terminal_outcome]
        raise HostUnhealthy(message, code=code)
    return permissions


def _peer_uid(connection: socket.socket) -> int:
    if sys.platform == "darwin":
        local_peercred = getattr(socket, "LOCAL_PEERCRED", 1)
        raw = connection.getsockopt(0, local_peercred, 128)
        if len(raw) < 8:
            raise _ServerError("could not authenticate client", code="peer_authentication_failed")
        version, uid = struct.unpack_from("=II", raw)
        if version != 0:
            raise _ServerError("unsupported peer credentials", code="peer_authentication_failed")
        return uid
    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is not None:
        raw = connection.getsockopt(socket.SOL_SOCKET, peercred, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    raise _ServerError("platform has no peer credential API", code="peer_authentication_failed")


def _canonical_unsymlinked(path: Path, *, label: str) -> Path:
    try:
        if path.is_symlink():
            raise _ServerError(f"{label} is a symlink", code="host_runtime_invalid")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _ServerError(f"{label} is unavailable", code="host_runtime_invalid") from exc
    if path.absolute() != resolved:
        raise _ServerError(f"{label} is not canonical", code="host_runtime_invalid")
    return resolved


def _hosted_runtime_from_environment() -> HostedRuntime:
    if os.environ.get(ACTIVE_ENV) != "1":
        raise _ServerError("host active marker is missing", code="host_runtime_invalid")
    try:
        archive_fd = int(os.environ[ARCHIVE_FD_ENV])
        cdhash = os.environ[HOST_CDHASH_ENV].strip().lower()
        app = Path(os.environ[APP_ENV]).expanduser().absolute()
        runtime = Path(os.environ[RUNTIME_ENV]).expanduser().absolute()
    except (KeyError, TypeError, ValueError) as exc:
        raise _ServerError("host runtime environment is incomplete", code="host_runtime_invalid") from exc
    if archive_fd < 3 or not _CDHASH_PATTERN.fullmatch(f"CDHash={cdhash}"):
        raise _ServerError("host archive or CDHash is invalid", code="host_runtime_invalid")
    try:
        archive_details = os.fstat(archive_fd)
    except OSError as exc:
        raise _ServerError("host archive descriptor is unavailable", code="host_runtime_invalid") from exc
    if not stat.S_ISREG(archive_details.st_mode):
        raise _ServerError("host archive descriptor is not a regular file", code="host_runtime_invalid")
    app = _canonical_unsymlinked(app, label="Capability Host app")
    runtime = _canonical_unsymlinked(runtime, label="Capability Host runtime")
    expected_runtime = app / "Contents/Resources/CapabilityRuntime"
    if runtime != expected_runtime:
        raise _ServerError("Capability Host runtime is outside the signed app", code="host_runtime_invalid")
    bridge = _canonical_unsymlinked(runtime / "bin/remctl-bridge", label="EventKit bridge")
    private = _canonical_unsymlinked(runtime / "bin/remctl-private", label="private helper")
    for helper in (bridge, private):
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise _ServerError("sealed helper is not executable", code="host_runtime_invalid")
    return HostedRuntime(
        app=app,
        runtime=runtime,
        archive_fd=archive_fd,
        cdhash=cdhash,
        python=Path(sys.executable).resolve(strict=True),
        bridge=bridge,
        private=private,
    )


def _verify_sealed_bundle(runtime: HostedRuntime) -> None:
    try:
        verified = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(runtime.app)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        described = subprocess.run(
            ["/usr/bin/codesign", "-dvvv", str(runtime.app)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _ServerError("could not verify signed host", code="host_signature_invalid") from exc
    match = _CDHASH_PATTERN.search(described.stderr.decode("utf-8", errors="replace"))
    if verified.returncode != 0 or described.returncode != 0 or match is None:
        raise _ServerError("signed host verification failed", code="host_signature_invalid")
    if match.group(1).lower() != runtime.cdhash:
        raise _ServerError("running host CDHash changed", code="host_signature_invalid")


def _process_executable_path(pid: int) -> Path:
    """Read one process executable path without invoking another process."""

    if sys.platform != "darwin":
        raise _ServerError(
            "Capability Host parent verification requires macOS",
            code="host_runtime_invalid",
        )
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidpath = library.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        length = proc_pidpath(pid, buffer, ctypes.sizeof(buffer))
    except (AttributeError, OSError) as exc:
        raise _ServerError(
            "could not inspect Capability Host parent",
            code="host_runtime_invalid",
        ) from exc
    if length <= 0 or not buffer.value:
        raise _ServerError(
            "could not identify Capability Host parent",
            code="host_runtime_invalid",
        )
    try:
        return Path(os.fsdecode(buffer.value)).resolve(strict=True)
    except OSError as exc:
        raise _ServerError(
            "Capability Host parent executable is unavailable",
            code="host_runtime_invalid",
        ) from exc


def _verified_parent_host_pid(runtime: HostedRuntime) -> int:
    """Verify that this broker is still a child of the signed native host."""

    _verify_sealed_bundle(runtime)
    parent_pid = os.getppid()
    if parent_pid <= 1:
        raise _ServerError(
            "Capability Host parent is unavailable",
            code="host_runtime_invalid",
        )
    expected = (runtime.app / "Contents/MacOS" / EXECUTABLE_NAME).resolve(strict=True)
    if _process_executable_path(parent_pid) != expected or os.getppid() != parent_pid:
        raise _ServerError(
            "broker parent is not the signed Capability Host",
            code="host_runtime_invalid",
        )
    return parent_pid


def _terminate_verified_parent_host(runtime: HostedRuntime, parent_pid: int) -> None:
    """Terminate only the still-current, expected native parent process."""

    expected = (runtime.app / "Contents/MacOS" / EXECUTABLE_NAME).resolve(strict=True)
    if (
        parent_pid <= 1
        or os.getppid() != parent_pid
        or _process_executable_path(parent_pid) != expected
        or os.getppid() != parent_pid
    ):
        return
    try:
        os.kill(parent_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _service_environment(
    runtime: HostedRuntime,
    scratch: Path,
    *,
    stdout_columns: int | None,
) -> dict[str, str]:
    home = str(Path.home())
    environment = {
        "HOME": home,
        "USER": os.environ.get("USER", str(os.getuid())),
        "LOGNAME": os.environ.get("LOGNAME", os.environ.get("USER", str(os.getuid()))),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(scratch),
        ACTIVE_ENV: "1",
        APP_ENV: str(runtime.app),
        RUNTIME_ENV: str(runtime.runtime),
        ARCHIVE_FD_ENV: str(runtime.archive_fd),
        HOST_CDHASH_ENV: runtime.cdhash,
        "REMCTL_SKIP_ONBOARD": "1",
        "REMCTL_BRIDGE_PATH": str(runtime.bridge),
        "REMCTL_PRIVATE_PATH": str(runtime.private),
        "REMCTL_STORE_DIR": str(
            Path(home) / "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores"
        ),
    }
    if stdout_columns is not None:
        environment.update(
            {
                "REMCTL_CAPABILITY_STDOUT_TTY": "1",
                "REMCTL_CAPABILITY_COLUMNS": str(stdout_columns),
                "REMCTL_IMAGES_FORCE": "1",
            }
        )
    for name in ("LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(name)
        if value and len(value) <= 128 and "\x00" not in value:
            environment[name] = value
    return environment


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _open_nonblocking_tty_writer(fd: int) -> int:
    """Reopen one validated terminal without changing the caller's file flags."""

    path = Path(os.ttyname(fd))
    if not path.is_absolute() or path.parent != Path("/dev"):
        raise OSError("terminal path is outside /dev")
    original = os.fstat(fd)
    flags = (
        os.O_WRONLY
        | os.O_NONBLOCK
        | getattr(os, "O_NOCTTY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    reopened = os.open(path, flags)
    try:
        details = os.fstat(reopened)
        if (
            not stat.S_ISCHR(details.st_mode)
            or not os.isatty(reopened)
            or details.st_rdev != original.st_rdev
        ):
            raise OSError("terminal changed while reopening")
        return reopened
    except Exception:
        os.close(reopened)
        raise


def _decode_run_signal_control(payload: bytes) -> int:
    """Validate the one allowed post-dispatch control frame."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate signal control key")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _ServerError(
            "run signal control is malformed",
            code="invalid_signal_control",
            dispatched=True,
            indeterminate=True,
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"protocolVersion", "operation", "signal"}
        or type(value.get("protocolVersion")) is not int
        or value.get("protocolVersion") != PROTOCOL_VERSION
        or value.get("operation") != "signal"
    ):
        raise _ServerError(
            "run signal control schema is invalid",
            code="invalid_signal_control",
            dispatched=True,
            indeterminate=True,
        )
    signals = {
        "SIGINT": int(signal.SIGINT),
        "SIGTERM": int(signal.SIGTERM),
    }
    try:
        return signals[value.get("signal")]
    except (KeyError, TypeError) as exc:
        raise _ServerError(
            "run signal control requested an unsupported signal",
            code="invalid_signal_control",
            dispatched=True,
            indeterminate=True,
        ) from exc


def _collect_bounded_output(
    process: subprocess.Popen[bytes],
    *,
    stdin_bytes: bytes,
    stderr_relay_fd: int | None,
    timeout: float,
    client_connection: socket.socket | None = None,
) -> tuple[int, bytes, bytes, bool]:
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    stream_totals = {"stdout": 0, "stderr": 0}
    control_buffer = bytearray()
    control_size: int | None = None
    control_deadline: float | None = None
    forwarded_signal: int | None = None
    forwarded_signal_deadline: float | None = None
    client_eof = False

    stderr_relay_failed = threading.Event()
    relay_output_fd: int | None = None
    if stderr_relay_fd is not None:
        try:
            relay_output_fd = _open_nonblocking_tty_writer(stderr_relay_fd)
        except OSError:
            stderr_relay_failed.set()

    streams: dict[int, tuple[str, Any, list[bytes]]] = {}
    for stream_name, stream, parts in (
        ("stdout", process.stdout, stdout_parts),
        ("stderr", process.stderr, stderr_parts),
    ):
        if stream is not None:
            streams[stream.fileno()] = (stream_name, stream, parts)

    writer: threading.Thread | None = None
    if process.stdin is not None:
        def write_stdin() -> None:
            try:
                process.stdin.write(stdin_bytes)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        writer = threading.Thread(target=write_stdin, daemon=True)
        writer.start()

    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None or streams:
            now = time.monotonic()
            if control_deadline is not None and now >= control_deadline:
                _terminate(process)
                raise _ServerError(
                    "run signal control frame timed out",
                    code="invalid_signal_control",
                    dispatched=True,
                    indeterminate=True,
                )
            if (
                forwarded_signal_deadline is not None
                and process.poll() is None
                and now >= forwarded_signal_deadline
            ):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                raise _ServerError(
                    "hosted command did not exit after the forwarded signal",
                    code="client_signal_timeout",
                    dispatched=True,
                    indeterminate=True,
                )
            if client_connection is not None and not client_eof:
                try:
                    client_readable, _, _ = select.select(
                        [client_connection], [], [], 0
                    )
                    control_chunk = (
                        client_connection.recv(SIGNAL_CONTROL_MAX_BYTES + 5)
                        if client_readable
                        else None
                    )
                except (OSError, ValueError) as exc:
                    _terminate(process)
                    raise _ServerError(
                        "client connection failed after command dispatch",
                        code="client_disconnected",
                        dispatched=True,
                        indeterminate=True,
                    ) from exc
                if control_chunk == b"":
                    client_eof = True
                    if control_buffer:
                        _terminate(process)
                        raise _ServerError(
                            "client closed during a signal control frame",
                            code="invalid_signal_control",
                            dispatched=True,
                            indeterminate=True,
                        )
                    if forwarded_signal is None:
                        _terminate(process)
                        raise _ServerError(
                            "client disconnected after command dispatch",
                            code="client_disconnected",
                            dispatched=True,
                            indeterminate=True,
                        )
                elif control_chunk:
                    if forwarded_signal is not None:
                        _terminate(process)
                        raise _ServerError(
                            "only one run signal control is allowed",
                            code="invalid_signal_control",
                            dispatched=True,
                            indeterminate=True,
                        )
                    if control_deadline is None:
                        control_deadline = now + SIGNAL_CONTROL_PARTIAL_TIMEOUT_SECONDS
                    control_buffer.extend(control_chunk)
                    if control_size is None and len(control_buffer) >= 4:
                        control_size = struct.unpack("!I", control_buffer[:4])[0]
                        if control_size == 0 or control_size > SIGNAL_CONTROL_MAX_BYTES:
                            _terminate(process)
                            raise _ServerError(
                                "run signal control frame length is invalid",
                                code="invalid_signal_control",
                                dispatched=True,
                                indeterminate=True,
                            )
                    if control_size is not None:
                        frame_size = 4 + control_size
                        if len(control_buffer) > frame_size:
                            _terminate(process)
                            raise _ServerError(
                                "run signal control has trailing bytes",
                                code="invalid_signal_control",
                                dispatched=True,
                                indeterminate=True,
                            )
                        if len(control_buffer) == frame_size:
                            forwarded_signal = _decode_run_signal_control(
                                bytes(control_buffer[4:])
                            )
                            control_buffer.clear()
                            control_size = None
                            control_deadline = None
                            try:
                                os.killpg(process.pid, forwarded_signal)
                            except (OSError, ProcessLookupError):
                                pass
                            forwarded_signal_deadline = (
                                time.monotonic() + SIGNAL_CONTROL_GRACE_SECONDS
                            )
            if now >= deadline:
                _terminate(process)
                raise _ServerError(
                    "hosted command timed out",
                    code="command_timeout",
                    dispatched=True,
                    indeterminate=True,
                )
            if not streams:
                time.sleep(0.02)
                continue
            readable, _, _ = select.select(list(streams), [], [], 0.02)
            for fd in readable:
                stream_name, stream, parts = streams[fd]
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    streams.pop(fd, None)
                    try:
                        stream.close()
                    except OSError:
                        pass
                    continue
                parts.append(chunk)
                stream_totals[stream_name] += len(chunk)
                if (
                    stream_totals["stdout"] > MAX_STDOUT_BYTES
                    or stream_totals["stderr"] > MAX_STDERR_BYTES
                    or stream_totals["stdout"] + stream_totals["stderr"]
                    > MAX_COMBINED_OUTPUT_BYTES
                ):
                    _terminate(process)
                    raise _ServerError(
                        "hosted command output exceeded its limit",
                        code="command_output_too_large",
                        dispatched=True,
                        indeterminate=True,
                    )
                if stream_name == "stderr" and relay_output_fd is not None:
                    view = memoryview(chunk)
                    try:
                        while view:
                            written = os.write(relay_output_fd, view)
                            if written <= 0:
                                raise OSError("terminal relay stopped")
                            view = view[written:]
                    except OSError:
                        stderr_relay_failed.set()
                        os.close(relay_output_fd)
                        relay_output_fd = None
    except _ServerError as exc:
        stdout = b"".join(stdout_parts)[:MAX_STDOUT_BYTES]
        stderr = b"".join(stderr_parts)[:MAX_STDERR_BYTES]
        remaining_for_stderr = max(0, MAX_COMBINED_OUTPUT_BYTES - len(stdout))
        exc.stdout = stdout
        exc.stderr = stderr[:remaining_for_stderr]
        exc.stderr_relayed = bool(
            relay_output_fd is not None and not stderr_relay_failed.is_set()
        )
        raise
    finally:
        for _fd, (_stream_name, stream, _parts) in list(streams.items()):
            try:
                stream.close()
            except OSError:
                pass
        streams.clear()
        if writer is not None:
            writer.join(timeout=2)
        if relay_output_fd is not None:
            try:
                os.close(relay_output_fd)
            except OSError:
                pass
    stdout = b"".join(stdout_parts)
    stderr = b"".join(stderr_parts)
    if (
        len(stdout) > MAX_STDOUT_BYTES
        or len(stderr) > MAX_STDERR_BYTES
        or len(stdout) + len(stderr) > MAX_COMBINED_OUTPUT_BYTES
    ):
        raise _ServerError(
            "hosted command output exceeded its limit",
            code="command_output_too_large",
            dispatched=True,
            indeterminate=True,
        )
    return int(process.returncode), stdout, stderr, bool(
        relay_output_fd is not None and not stderr_relay_failed.is_set()
    )


def _run_cli(
    argv: list[str],
    *,
    runtime: HostedRuntime,
    capabilities: dict[str, DescriptorCapability],
    stdin_bytes: bytes,
    timeout: float,
    client_connection: socket.socket | None = None,
    merge_output: bool = False,
) -> dict[str, Any]:
    process: subprocess.Popen[bytes] | None = None
    launch_gate = threading.Lock()
    dispatch_started = threading.Event()
    monitor_stop = threading.Event()
    cancellation_errors: list[_ServerError] = []
    monitor: threading.Thread | None = None

    if client_connection is not None:
        def monitor_predispatch_cancellation() -> None:
            while not monitor_stop.is_set():
                try:
                    readable, _, _ = select.select(
                        [client_connection], [], [], 0.01
                    )
                except (OSError, ValueError) as exc:
                    with launch_gate:
                        if not dispatch_started.is_set():
                            cancellation_errors.append(
                                _ServerError(
                                    "client connection failed before command dispatch",
                                    code="client_disconnected",
                                )
                            )
                    return
                if not readable:
                    continue
                with launch_gate:
                    if dispatch_started.is_set():
                        return
                    try:
                        _reject_or_cancel_before_dispatch(client_connection)
                    except _ServerError as exc:
                        cancellation_errors.append(exc)
                        monitor_stop.set()
                        return

        monitor = threading.Thread(
            target=monitor_predispatch_cancellation,
            daemon=True,
        )
        monitor.start()
    try:
        _verify_sealed_bundle(runtime)
        validate_argv(argv, parser=_load_real_parser())
        with tempfile.TemporaryDirectory(prefix="remctl-capability-host-") as scratch_value:
            scratch = Path(scratch_value)
            scratch.chmod(0o700)
            rewritten, tty_fd, stdout_columns, stderr_fd = materialize_inputs(
                argv, capabilities, scratch / "inputs"
            )
            if merge_output and (
                tty_fd is not None
                or stderr_fd is not None
                or any(
                    capability.kind == "tty"
                    and capability.purpose in {"stdin", "stderr"}
                    for capability in capabilities.values()
                )
            ):
                raise _ServerError(
                    "merged output is unavailable for an interactive command",
                    code="invalid_capabilities",
                )
            environment = _service_environment(
                runtime,
                scratch,
                stdout_columns=stdout_columns,
            )
            command = [
                str(runtime.python),
                "-I",
                "-S",
                f"/dev/fd/{runtime.archive_fd}",
                "cli",
                *rewritten,
            ]
            pass_fds = (runtime.archive_fd,)
            stdin: Any = subprocess.PIPE
            if tty_fd is not None:
                stdin = tty_fd
                pass_fds = (runtime.archive_fd, tty_fd)
            with launch_gate:
                if cancellation_errors:
                    raise cancellation_errors[0]
                if client_connection is not None:
                    _reject_or_cancel_before_dispatch(client_connection)
                dispatch_started.set()
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=stdin,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT if merge_output else subprocess.PIPE,
                        cwd=runtime.runtime,
                        env=environment,
                        pass_fds=pass_fds,
                        start_new_session=True,
                    )
                except OSError as exc:
                    raise _ServerError(
                        "could not start sealed RemCTL",
                        code="command_spawn_failed",
                    ) from exc
            monitor_stop.set()
            if monitor is not None:
                monitor.join(timeout=1)
            with _ACTIVE_PROCESSES_LOCK:
                _ACTIVE_PROCESSES.add(process)
            try:
                exit_code, stdout, stderr, stderr_relayed = _collect_bounded_output(
                    process,
                    stdin_bytes=stdin_bytes,
                    stderr_relay_fd=stderr_fd,
                    timeout=timeout,
                    client_connection=client_connection,
                )
            finally:
                with _ACTIVE_PROCESSES_LOCK:
                    _ACTIVE_PROCESSES.discard(process)
            try:
                _completion_signal(exit_code)
            except HostIndeterminate as exc:
                raise _ServerError(
                    str(exc),
                    code="invalid_child_exit_status",
                    dispatched=True,
                    indeterminate=True,
                ) from exc
            return {
                "status": "completed",
                "protocolVersion": PROTOCOL_VERSION,
                "dispatched": True,
                "indeterminate": False,
                "exitCode": exit_code,
                "stdoutBase64": base64.b64encode(stdout).decode("ascii"),
                "stderrBase64": base64.b64encode(stderr).decode("ascii"),
                "stderrRelayed": stderr_relayed,
            }
    except Exception as exc:
        if process is None:
            raise
        try:
            if process.poll() is None:
                _terminate(process)
        except Exception:
            pass
        try:
            with _ACTIVE_PROCESSES_LOCK:
                _ACTIVE_PROCESSES.discard(process)
        except Exception:
            pass
        if isinstance(exc, _ServerError) and exc.dispatched and exc.indeterminate:
            raise
        raise _ServerError(
            str(exc) or "hosted command result became indeterminate",
            code=getattr(exc, "code", "command_result_indeterminate"),
            dispatched=True,
            indeterminate=True,
        ) from exc
    finally:
        monitor_stop.set()
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=1)


def _native_permission_socket() -> socket.socket:
    """Duplicate and validate the private channel inherited from the native host."""

    if os.environ.get(ACTIVE_ENV) != "1":
        raise _ServerError(
            "native permission channel requires the active host",
            code="permission_channel_unavailable",
        )
    if os.environ.get(NATIVE_FD_ENV) != str(NATIVE_PERMISSION_FD):
        raise _ServerError(
            "native permission channel descriptor is missing",
            code="permission_channel_unavailable",
        )
    try:
        details = os.fstat(NATIVE_PERMISSION_FD)
    except OSError as exc:
        raise _ServerError(
            "native permission channel descriptor is unavailable",
            code="permission_channel_unavailable",
        ) from exc
    if not stat.S_ISSOCK(details.st_mode):
        raise _ServerError(
            "native permission channel descriptor is not a socket",
            code="permission_channel_invalid",
        )
    try:
        duplicate = os.dup(NATIVE_PERMISSION_FD)
        os.set_inheritable(duplicate, False)
    except OSError as exc:
        raise _ServerError(
            "native permission channel could not be opened",
            code="permission_channel_unavailable",
        ) from exc
    try:
        channel = socket.socket(fileno=duplicate)
    except OSError as exc:
        os.close(duplicate)
        raise _ServerError(
            "native permission channel is not a socket",
            code="permission_channel_invalid",
        ) from exc
    try:
        if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_STREAM:
            raise _ServerError(
                "native permission channel has the wrong socket type",
                code="permission_channel_invalid",
            )
        if channel.getsockname() not in ("", b"") or channel.getpeername() not in ("", b""):
            raise _ServerError(
                "native permission channel is not an anonymous socket pair",
                code="permission_channel_invalid",
            )
    except Exception:
        channel.close()
        raise
    return channel


def _native_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ServerError(
            "native permission request timed out",
            code="permission_channel_timeout",
        )
    return remaining


def _send_native_bytes(
    channel: socket.socket,
    data: bytes,
    *,
    deadline: float,
    dispatch_started: threading.Event | None = None,
) -> None:
    offset = 0
    while offset < len(data):
        channel.settimeout(_native_remaining(deadline))
        sent = channel.send(data[offset:])
        if sent <= 0:
            raise _ServerError(
                "native permission channel closed while sending",
                code="permission_channel_failed",
            )
        if dispatch_started is not None:
            dispatch_started.set()
        offset += sent


def _recv_native_bytes(channel: socket.socket, size: int, *, deadline: float) -> bytes:
    parts: list[bytes] = []
    remaining_size = size
    while remaining_size:
        channel.settimeout(_native_remaining(deadline))
        part = channel.recv(remaining_size)
        if not part:
            raise _ServerError(
                "native permission channel closed while receiving",
                code="permission_channel_failed",
            )
        parts.append(part)
        remaining_size -= len(part)
    return b"".join(parts)


def _native_json_object(data: bytes) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        payload = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _ServerError(
            "native permission response is not valid JSON",
            code="permission_response_invalid",
        ) from exc
    if not isinstance(payload, dict):
        raise _ServerError(
            "native permission response must be an object",
            code="permission_response_invalid",
        )
    return payload


def _bounded_native_string(value: Any, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and "\x00" not in value
        and all(character >= " " or character in "\t\n" for character in value)
    )


def _validate_native_permission_response(payload: dict[str, Any]) -> dict[str, Any]:
    protocol_version = payload.get("protocolVersion")
    if type(protocol_version) is not int or protocol_version != NATIVE_PROTOCOL_VERSION:
        raise _ServerError(
            "native permission protocol version is invalid",
            code="permission_response_invalid",
        )
    status_value = payload.get("status")
    if status_value == "error":
        if set(payload) != {"protocolVersion", "status", "code", "message"}:
            raise _ServerError(
                "native permission error schema is invalid",
                code="permission_response_invalid",
            )
        code = payload.get("code")
        message = payload.get("message")
        if (
            not isinstance(code, str)
            or _NATIVE_ERROR_CODE_PATTERN.fullmatch(code) is None
            or not _bounded_native_string(message, maximum=2000)
        ):
            raise _ServerError(
                "native permission error fields are invalid",
                code="permission_response_invalid",
            )
        raise _NativePermissionError(message, code=code)
    if status_value != "ok" or set(payload) != {
        "protocolVersion",
        "status",
        "permissions",
    }:
        raise _ServerError(
            "native permission response schema is invalid",
            code="permission_response_invalid",
        )
    permissions = payload.get("permissions")
    expected_permission_keys = {
        "status",
        "fullDiskAccess",
        "reminders",
        "automation",
        "automationTarget",
    }
    if not isinstance(permissions, dict) or set(permissions) != expected_permission_keys:
        raise _ServerError(
            "native permission fields are invalid",
            code="permission_response_invalid",
        )
    if permissions.get("status") != "ok" or permissions.get("automationTarget") != "com.apple.reminders":
        raise _ServerError(
            "native permission identity is invalid",
            code="permission_response_invalid",
        )
    if any(
        not _bounded_native_string(permissions.get(key), maximum=128)
        for key in ("fullDiskAccess", "reminders", "automation")
    ):
        raise _ServerError(
            "native permission values are invalid",
            code="permission_response_invalid",
        )
    return permissions


def _reject_native_trailing_bytes(channel: socket.socket) -> None:
    """Reject unsolicited bytes after the one serialized native response."""

    try:
        readable, _, _ = select.select([channel], [], [], 0)
        if not readable:
            return
        trailing = channel.recv(1, socket.MSG_PEEK)
    except (OSError, ValueError) as exc:
        raise _ServerError(
            "native permission channel could not validate response framing",
            code="permission_channel_failed",
        ) from exc
    if trailing:
        raise _ServerError(
            "native permission channel returned trailing bytes",
            code="permission_response_invalid",
        )
    raise _ServerError(
        "native permission channel closed after its response",
        code="permission_channel_failed",
    )


def _native_permission_command(
    runtime: HostedRuntime,
    operation: str,
    *,
    timeout: float | None = None,
    verify_bundle: bool = True,
) -> dict[str, Any]:
    """Run one fixed permission operation inside the persistent native host."""

    allowed_operations = {
        "permissionStatus": 10.0,
        "requestReminders": 300.0,
        "requestAutomation": 300.0,
    }
    if operation not in allowed_operations:
        raise _ServerError(
            "unsupported native permission operation",
            code="invalid_permission_operation",
        )
    request_timeout = allowed_operations[operation] if timeout is None else timeout
    if (
        not isinstance(request_timeout, (int, float))
        or isinstance(request_timeout, bool)
        or request_timeout <= 0
    ):
        raise _ServerError(
            "native permission timeout is invalid",
            code="permission_channel_invalid",
        )
    if verify_bundle:
        _verify_sealed_bundle(runtime)
    queue_timeout = min(
        float(request_timeout),
        NATIVE_PERMISSION_QUEUE_TIMEOUT_SECONDS,
    )
    acquired = _NATIVE_PERMISSION_LOCK.acquire(timeout=queue_timeout)
    if not acquired:
        raise _ServerError(
            "native permission channel is busy",
            code="permission_channel_timeout",
        )
    channel: socket.socket | None = None
    dispatch_started = threading.Event()
    try:
        deadline = time.monotonic() + float(request_timeout)
        if _NATIVE_PERMISSION_CHANNEL_FAILED.is_set():
            raise _ServerError(
                "native permission channel previously failed",
                code="permission_channel_failed",
            )
        channel = _native_permission_socket()
        request = json.dumps(
            {
                "protocolVersion": NATIVE_PROTOCOL_VERSION,
                "operation": operation,
            },
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        if not request or len(request) > MAX_NATIVE_REQUEST_BYTES:
            raise _ServerError(
                "native permission request is too large",
                code="permission_channel_invalid",
            )
        frame = struct.pack("!I", len(request)) + request
        _send_native_bytes(
            channel,
            frame,
            deadline=deadline,
            dispatch_started=dispatch_started,
        )
        header = _recv_native_bytes(channel, 4, deadline=deadline)
        response_size = struct.unpack("!I", header)[0]
        if response_size == 0 or response_size > MAX_NATIVE_RESPONSE_BYTES:
            raise _ServerError(
                "native permission response frame is invalid",
                code="permission_response_invalid",
            )
        response = _recv_native_bytes(channel, response_size, deadline=deadline)
        _reject_native_trailing_bytes(channel)
        return _validate_native_permission_response(_native_json_object(response))
    except _NativePermissionError:
        raise
    except _ServerError as exc:
        if dispatch_started.is_set() and exc.code in {
            "permission_channel_failed",
            "permission_channel_timeout",
            "permission_response_invalid",
        }:
            _NATIVE_PERMISSION_CHANNEL_FAILED.set()
        if dispatch_started.is_set():
            raise _ServerError(
                str(exc),
                code=exc.code,
                dispatched=True,
                indeterminate=True,
            ) from exc
        raise
    except (OSError, socket.timeout, struct.error) as exc:
        code = "permission_channel_timeout" if isinstance(exc, socket.timeout) else "permission_channel_failed"
        if dispatch_started.is_set():
            _NATIVE_PERMISSION_CHANNEL_FAILED.set()
        raise _ServerError(
            "native permission channel failed",
            code=code,
            dispatched=dispatch_started.is_set(),
            indeterminate=dispatch_started.is_set(),
        ) from exc
    finally:
        if channel is not None:
            try:
                channel.settimeout(None)
            except OSError:
                pass
            channel.close()
        _NATIVE_PERMISSION_LOCK.release()


def _request_native_permission_with_client_lifecycle(
    runtime: HostedRuntime,
    operation: str,
    connection: socket.socket,
) -> dict[str, Any]:
    """Cancel the native permission gate when its public client goes away."""

    def reject_cancelled_client() -> None:
        try:
            readable, _, _ = select.select([connection], [], [], 0)
            if not readable:
                return
            pending = connection.recv(1, socket.MSG_PEEK)
        except (OSError, ValueError) as exc:
            raise _ServerError(
                "permission client connection failed before dispatch",
                code="client_disconnected",
            ) from exc
        if pending:
            raise _ServerError(
                "permission client sent data before dispatch",
                code="invalid_frame",
            )
        raise _ServerError(
            "permission client disconnected before dispatch",
            code="client_disconnected",
        )

    reject_cancelled_client()
    launch_gate = threading.Lock()
    dispatch_started = threading.Event()
    monitor_ready = threading.Event()
    stop_monitor = threading.Event()
    cancellation_errors: list[_ServerError] = []
    parent_pid: list[int] = []

    def monitor_client() -> None:
        monitor_ready.set()
        while not stop_monitor.is_set():
            try:
                readable, _, _ = select.select([connection], [], [], 0.05)
                if not readable:
                    continue
                try:
                    reject_cancelled_client()
                except _ServerError as exc:
                    connection_error = exc
                else:
                    continue
            except (OSError, ValueError) as exc:
                connection_error = _ServerError(
                    "permission client connection failed",
                    code="client_disconnected",
                )
            with launch_gate:
                if not dispatch_started.is_set():
                    cancellation_errors.append(connection_error)
                    stop_monitor.set()
                    return
                verified_parent = parent_pid[0]
            try:
                _terminate_verified_parent_host(runtime, verified_parent)
            except (_ServerError, OSError):
                pass
            return

    monitor = threading.Thread(
        target=monitor_client,
        name=f"remctl-permission-client-{id(connection)}",
        daemon=True,
    )
    monitor.start()
    try:
        if not monitor_ready.wait(timeout=1):
            raise _ServerError(
                "permission client monitor did not start",
                code="permission_monitor_failed",
            )
        verified_parent = _verified_parent_host_pid(runtime)
        with launch_gate:
            if cancellation_errors:
                raise cancellation_errors[0]
            reject_cancelled_client()
            parent_pid.append(verified_parent)
            dispatch_started.set()
        return _native_permission_command(runtime, operation)
    finally:
        stop_monitor.set()
        monitor.join(timeout=1)


def _permission_cache_key(runtime: HostedRuntime) -> tuple[str, str]:
    return str(runtime.app), runtime.cdhash


def _native_status_after_verification(
    runtime: HostedRuntime,
    parent_pid: int,
) -> dict[str, Any]:
    """Read native status after strict verification, restarting on desync risk."""

    try:
        return _native_permission_command(
            runtime,
            "permissionStatus",
            timeout=PERMISSION_STATUS_TIMEOUT_SECONDS,
            verify_bundle=False,
        )
    except _ServerError as exc:
        if exc.dispatched or exc.indeterminate:
            _NATIVE_PERMISSION_CHANNEL_FAILED.set()
            try:
                _terminate_verified_parent_host(runtime, parent_pid)
            except (_ServerError, OSError):
                pass
        raise


def _refresh_verified_status(
    runtime: HostedRuntime,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Verify current disk state before native status and private-helper execution."""

    parent_pid = _verified_parent_host_pid(runtime)
    permissions = _native_status_after_verification(runtime, parent_pid)
    return parent_pid, permissions, _private_protocol(runtime)


def _remember_permission_status(
    runtime: HostedRuntime,
    permissions: dict[str, Any],
) -> None:
    """Cache validated native results from an explicit permission request."""

    key = _permission_cache_key(runtime)
    with _PERMISSION_STATUS_LOCK:
        _PERMISSION_STATUS_GENERATION[key] = _PERMISSION_STATUS_GENERATION.get(key, 0) + 1
        _PERMISSION_STATUS_CACHE[key] = (
            time.monotonic(),
            dict(permissions),
        )


def _full_disk_access_status() -> str:
    """Probe the effective broker process's bounded read access to the store."""

    stores = (
        Path.home()
        / "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores"
    )
    try:
        with os.scandir(stores) as entries:
            for index, item in enumerate(entries):
                if index >= 1024:
                    break
                if not item.name.startswith("Data-") or not item.name.endswith(".sqlite"):
                    continue
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    descriptor = os.open(item.path, flags)
                except OSError:
                    continue
                try:
                    if stat.S_ISREG(os.fstat(descriptor).st_mode):
                        os.read(descriptor, 1)
                        return "authorized"
                except OSError:
                    pass
                finally:
                    os.close(descriptor)
    except OSError:
        pass
    return "denied"


def _with_effective_full_disk_access(permissions: dict[str, Any]) -> dict[str, Any]:
    result = dict(permissions)
    result["fullDiskAccess"] = _full_disk_access_status()
    return result


def _unknown_permission_status() -> dict[str, Any]:
    return {
        "status": "ok",
        "fullDiskAccess": _full_disk_access_status(),
        "reminders": "unknown",
        "automation": "unknown",
        "automationTarget": "com.apple.reminders",
    }


def _private_protocol_snapshot(runtime: HostedRuntime) -> dict[str, Any]:
    """Return only a private-helper result produced after strict verification."""

    key = _permission_cache_key(runtime)
    with _PERMISSION_STATUS_LOCK:
        cached = _PRIVATE_PROTOCOL_CACHE.get(key)
        failure = _PERMISSION_STATUS_FAILURES.get(key)
    now = time.monotonic()
    if cached is not None and (
        now - cached[0] <= PERMISSION_STATUS_TTL_SECONDS
        or (
            failure is not None
            and now - failure[0] <= PERMISSION_STATUS_FAILURE_BACKOFF_SECONDS
        )
    ):
        return dict(cached[1])
    _schedule_permission_status_refresh(runtime)
    if cached is not None:
        return dict(cached[1])
    return {
        "compatible": False,
        "version": None,
        "error": "signed helper verification is pending",
    }


def _schedule_permission_status_refresh(runtime: HostedRuntime) -> threading.Event:
    """Start at most one status refresh without blocking the public request."""

    key = _permission_cache_key(runtime)
    with _PERMISSION_STATUS_LOCK:
        existing = _PERMISSION_STATUS_REFRESHES.get(key)
        if existing is not None:
            return existing
        completed = threading.Event()
        if _NATIVE_PERMISSION_CHANNEL_FAILED.is_set():
            completed.set()
            return completed
        failure = _PERMISSION_STATUS_FAILURES.get(key)
        if (
            failure is not None
            and time.monotonic() - failure[0] <= PERMISSION_STATUS_FAILURE_BACKOFF_SECONDS
        ):
            completed.set()
            return completed
        generation = _PERMISSION_STATUS_GENERATION.get(key, 0)
        _PERMISSION_STATUS_REFRESHES[key] = completed

    def refresh() -> None:
        error: Exception | None = None
        permissions: dict[str, Any] | None = None
        private_protocol: dict[str, Any] | None = None
        try:
            parent_pid, permissions, private_protocol = _refresh_verified_status(runtime)
            with _PERMISSION_STATUS_LOCK:
                _PRIVATE_PROTOCOL_CACHE[key] = (
                    time.monotonic(),
                    dict(private_protocol),
                )
                _PERMISSION_STATUS_FAILURES.pop(key, None)
            deadline = time.monotonic() + PERMISSION_STATUS_HYDRATION_SECONDS
            delay = 0.25
            while permissions.get("automation") == "unknown":
                with _PERMISSION_STATUS_LOCK:
                    if _PERMISSION_STATUS_GENERATION.get(key, 0) != generation:
                        return
                    _PERMISSION_STATUS_CACHE[key] = (
                        time.monotonic(),
                        dict(permissions),
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(delay, remaining))
                permissions = _native_status_after_verification(runtime, parent_pid)
                delay = min(delay * 2, 2.0)
        except Exception as exc:
            error = exc
        finally:
            with _PERMISSION_STATUS_LOCK:
                if _PERMISSION_STATUS_GENERATION.get(key, 0) == generation:
                    if permissions is not None:
                        _PERMISSION_STATUS_CACHE[key] = (
                            time.monotonic(),
                            dict(permissions),
                        )
                if error is not None:
                    message = str(error)[:2000] or error.__class__.__name__
                    _PRIVATE_PROTOCOL_CACHE[key] = (
                        time.monotonic(),
                        {
                            "compatible": False,
                            "version": None,
                            "error": message,
                        },
                    )
                    _PERMISSION_STATUS_FAILURES[key] = (
                        time.monotonic(),
                        message,
                    )
                _PERMISSION_STATUS_REFRESHES.pop(key, None)
                completed.set()

    threading.Thread(
        target=refresh,
        name=f"remctl-permission-status-{abs(hash(key))}",
        daemon=True,
    ).start()
    return completed


def _permission_status_snapshot(runtime: HostedRuntime) -> dict[str, Any]:
    """Return cached status while one bounded native refresh runs off-thread."""

    key = _permission_cache_key(runtime)
    now = time.monotonic()
    with _PERMISSION_STATUS_LOCK:
        cached = _PERMISSION_STATUS_CACHE.get(key)
        if cached is not None and now - cached[0] <= PERMISSION_STATUS_TTL_SECONDS:
            return _with_effective_full_disk_access(cached[1])
    completed = _schedule_permission_status_refresh(runtime)
    if cached is not None:
        return _with_effective_full_disk_access(cached[1])
    completed.wait(timeout=PERMISSION_STATUS_INITIAL_WAIT_SECONDS)
    with _PERMISSION_STATUS_LOCK:
        refreshed = _PERMISSION_STATUS_CACHE.get(key)
        if refreshed is not None:
            return _with_effective_full_disk_access(refreshed[1])
    return _unknown_permission_status()


def _private_protocol(runtime: HostedRuntime) -> dict[str, Any]:
    try:
        process = subprocess.run(
            [str(runtime.private)],
            input=b'{"action":"protocol_version"}',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        payload = json.loads(process.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"compatible": False, "version": None, "error": str(exc)}
    version = payload.get("protocolVersion") if isinstance(payload, dict) else None
    return {"compatible": version == PRIVATE_PROTOCOL_VERSION, "version": version}


def _server_status(runtime: HostedRuntime) -> dict[str, Any]:
    permissions = _permission_status_snapshot(runtime)
    private_protocol = _private_protocol_snapshot(runtime)
    command_scope = validate_command_scope_for_server()
    full_disk = permissions.get("fullDiskAccess") == "authorized"
    reminders = permissions.get("reminders") == "authorized"
    automation = permissions.get("automation") == "authorized"
    ready = full_disk
    full_ready = bool(
        ready
        and reminders
        and automation
        and private_protocol.get("compatible") is True
        and runtime.bridge.is_file()
    )
    return {
        "status": "ok" if ready else "error",
        "available": True,
        "ready": ready,
        "fullReady": full_ready,
        "protocolVersion": PROTOCOL_VERSION,
        "expectedProtocolVersion": PROTOCOL_VERSION,
        "scope": SCOPE,
        "supportsStdin": True,
        "supportsTTYStdin": True,
        "supportsWorkingDirectory": False,
        "permissions": permissions,
        "privateProtocol": private_protocol,
        "commandScope": command_scope,
        "error": None if ready else "Full Disk Access is not authorized",
    }


def validate_command_scope_for_server() -> dict[str, list[str]]:
    from remctl_capability_policy import validate_command_scope

    return validate_command_scope(_load_real_parser())


def _validate_request(request: dict[str, Any]) -> str:
    version = request.get("protocolVersion")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise _ServerError("Capability Host protocol mismatch", code="protocol_mismatch")
    operation = request.get("operation")
    if not isinstance(operation, str):
        raise _ServerError("broker operation is missing", code="invalid_request")
    try:
        allowed = request_schema(operation)
    except CapabilityPolicyError as exc:
        raise _ServerError(str(exc), code="invalid_request") from exc
    if set(request) != set(allowed):
        raise _ServerError("broker request fields are invalid", code="invalid_request")
    return operation


def _client_disconnected(connection: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        return bool(readable) and connection.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def _reject_pipelined_request(connection: socket.socket) -> None:
    """Reject bytes after the one allowed frame before any operation dispatches."""

    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if readable and connection.recv(1, socket.MSG_PEEK):
            raise _ServerError(
                "only one request frame is allowed per connection",
                code="invalid_frame",
            )
    except _ServerError:
        raise
    except (OSError, ValueError) as exc:
        raise _ServerError("could not validate request framing", code="invalid_frame") from exc


def _reject_or_cancel_before_dispatch(connection: socket.socket) -> None:
    """Consume one queued cancellation frame or reject any other pre-dispatch input."""

    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return
        first = connection.recv(SIGNAL_CONTROL_MAX_BYTES + 5)
    except (OSError, ValueError) as exc:
        raise _ServerError(
            "client connection failed before command dispatch",
            code="client_disconnected",
        ) from exc
    if not first:
        raise _ServerError(
            "client disconnected before command dispatch",
            code="client_disconnected",
        )
    frame = bytearray(first)
    deadline = time.monotonic() + SIGNAL_CONTROL_PARTIAL_TIMEOUT_SECONDS
    while len(frame) < 4:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ServerError(
                "pre-dispatch signal control frame timed out",
                code="invalid_signal_control",
            )
        readable, _, _ = select.select([connection], [], [], remaining)
        if not readable:
            continue
        chunk = connection.recv(SIGNAL_CONTROL_MAX_BYTES + 5 - len(frame))
        if not chunk:
            raise _ServerError(
                "client closed during a pre-dispatch signal control frame",
                code="invalid_signal_control",
            )
        frame.extend(chunk)
    body_size = struct.unpack("!I", frame[:4])[0]
    if body_size == 0 or body_size > SIGNAL_CONTROL_MAX_BYTES:
        raise _ServerError(
            "pre-dispatch signal control frame length is invalid",
            code="invalid_signal_control",
        )
    frame_size = 4 + body_size
    if len(frame) > frame_size:
        raise _ServerError(
            "pre-dispatch signal control has trailing bytes",
            code="invalid_signal_control",
        )
    while len(frame) < frame_size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ServerError(
                "pre-dispatch signal control frame timed out",
                code="invalid_signal_control",
            )
        readable, _, _ = select.select([connection], [], [], remaining)
        if not readable:
            continue
        chunk = connection.recv(frame_size - len(frame))
        if not chunk:
            raise _ServerError(
                "client closed during a pre-dispatch signal control frame",
                code="invalid_signal_control",
            )
        frame.extend(chunk)
    try:
        _decode_run_signal_control(bytes(frame[4:]))
    except _ServerError as exc:
        raise _ServerError(str(exc), code=exc.code) from exc
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if readable and connection.recv(1, socket.MSG_PEEK):
            raise _ServerError(
                "only one pre-dispatch signal control is allowed",
                code="invalid_signal_control",
            )
    except _ServerError:
        raise
    except (OSError, ValueError) as exc:
        raise _ServerError(
            "could not validate pre-dispatch signal framing",
            code="invalid_signal_control",
        ) from exc
    raise _ServerError(
        "client cancelled before command dispatch",
        code="client_cancelled_before_dispatch",
    )


def _handle_request(
    request: dict[str, Any],
    *,
    descriptors: list[int],
    runtime: HostedRuntime,
    connection: socket.socket,
) -> dict[str, Any]:
    operation = _validate_request(request)
    if operation == "ping":
        _verify_sealed_bundle(runtime)
        return {"status": "ok", "protocolVersion": PROTOCOL_VERSION}
    if operation == "status":
        return _server_status(runtime)
    if operation == "permissionStatus":
        return {
            "status": "ok",
            "protocolVersion": PROTOCOL_VERSION,
            "permissions": _permission_status_snapshot(runtime),
        }
    if operation == "requestPermission":
        permission = request.get("permission")
        operations = {
            "reminders": "requestReminders",
            "automation": "requestAutomation",
        }
        if permission not in operations:
            raise _ServerError("unsupported permission request", code="invalid_request")
        permissions = _request_native_permission_with_client_lifecycle(
            runtime,
            operations[permission],
            connection,
        )
        _remember_permission_status(runtime, permissions)
        return {
            "status": "ok",
            "protocolVersion": PROTOCOL_VERSION,
            "permissions": permissions,
        }

    argv = request.get("argv")
    if not isinstance(argv, list):
        raise _ServerError("run argv must be an array", code="invalid_request")
    try:
        _validated_argv, parsed_args = validate_argv(argv, parser=_load_real_parser())
    except CapabilityPolicyError as exc:
        raise _ServerError(str(exc), code="invalid_request") from exc
    try:
        capability_map = validate_received_capabilities(
            request.get("capabilities"), descriptors
        )
        validate_capability_bindings(argv, parsed_args, capability_map)
    except (CapabilityError, OSError) as exc:
        raise _ServerError(str(exc), code="invalid_capabilities") from exc
    try:
        stdin_bytes = base64.b64decode(request.get("stdinBase64", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise _ServerError("standard input is not valid base64", code="invalid_request") from exc
    if len(stdin_bytes) > MAX_STDIN_BYTES:
        raise _ServerError("standard input exceeds 8 MiB", code="invalid_request")
    merge_output = request.get("mergeOutput")
    if not isinstance(merge_output, bool):
        raise _ServerError("merged output flag is invalid", code="invalid_request")
    deadline = request.get("deadlineEpoch")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        raise _ServerError("run deadline is invalid", code="invalid_request")
    remaining = min(float(deadline) - time.time(), COMMAND_TIMEOUT_SECONDS)
    if remaining <= 0:
        raise _ServerError("run request expired before dispatch", code="request_expired")
    acquired = False
    try:
        while not acquired:
            _reject_or_cancel_before_dispatch(connection)
            remaining = min(float(deadline) - time.time(), COMMAND_TIMEOUT_SECONDS)
            if remaining <= 0:
                raise _ServerError(
                    "run request expired before dispatch",
                    code="request_expired",
                )
            acquired = _DISPATCH_LOCK.acquire(timeout=min(0.05, remaining))
        _reject_or_cancel_before_dispatch(connection)
        remaining = min(float(deadline) - time.time(), COMMAND_TIMEOUT_SECONDS)
        if remaining <= 0:
            raise _ServerError("run request expired before dispatch", code="request_expired")
        if _SERVICE_STOPPING.is_set():
            raise _ServerError("client disconnected before dispatch", code="client_disconnected")
        return _run_cli(
            argv,
            runtime=runtime,
            capabilities=capability_map,
            stdin_bytes=stdin_bytes,
            timeout=remaining,
            client_connection=connection,
            merge_output=merge_output,
        )
    finally:
        if acquired:
            _DISPATCH_LOCK.release()


def _error_response(exc: BaseException) -> dict[str, Any]:
    response = {
        "status": "error",
        "protocolVersion": PROTOCOL_VERSION,
        "code": getattr(exc, "code", "capability_host_error"),
        "message": str(exc)[:2000] or exc.__class__.__name__,
        "dispatched": bool(getattr(exc, "dispatched", False)),
        "indeterminate": bool(getattr(exc, "indeterminate", False)),
    }
    stdout = getattr(exc, "stdout", b"")
    stderr = getattr(exc, "stderr", b"")
    if stdout or stderr:
        response.update(
            {
                "stdoutBase64": base64.b64encode(stdout).decode("ascii"),
                "stderrBase64": base64.b64encode(stderr).decode("ascii"),
                "stderrRelayed": bool(getattr(exc, "stderr_relayed", False)),
            }
        )
    return response


def _prepare_socket(path: Path) -> socket.socket:
    if sys.platform == "darwin" and len(os.fsencode(path)) > 103:
        raise _ServerError("socket path exceeds macOS limit", code="unsafe_socket")
    parent = path.parent
    try:
        canonical_parent = parent.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _ServerError("socket parent cannot be resolved", code="unsafe_socket") from exc
    if not path.is_absolute() or parent != canonical_parent:
        raise _ServerError(
            "socket parent path must be absolute and canonical",
            code="unsafe_socket",
        )
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent != parent.resolve(strict=True):
        raise _ServerError("socket parent has symlinked ancestry", code="unsafe_socket")
    parent_details = parent.lstat()
    if not stat.S_ISDIR(parent_details.st_mode) or parent_details.st_uid != os.getuid():
        raise _ServerError("socket parent has unsafe ownership", code="unsafe_socket")
    os.chmod(parent, 0o700)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if (
            not stat.S_ISSOCK(existing.st_mode)
            or existing.st_uid != os.getuid()
            or stat.S_IMODE(existing.st_mode) != 0o600
        ):
            raise _ServerError("socket path is occupied by an unsafe object", code="unsafe_socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(path))
        except OSError:
            current = path.lstat()
            if (
                not stat.S_ISSOCK(current.st_mode)
                or current.st_uid != os.getuid()
                or (current.st_dev, current.st_ino) != (existing.st_dev, existing.st_ino)
            ):
                raise _ServerError("stale socket changed before cleanup", code="unsafe_socket")
            path.unlink()
        else:
            raise _ServerError("Capability Host is already running", code="already_running")
        finally:
            probe.close()
    old_umask = os.umask(0o077)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(16)
    except Exception:
        listener.close()
        raise
    finally:
        os.umask(old_umask)
    return listener


def serve(*, broker_socket: Path) -> None:
    runtime = _hosted_runtime_from_environment()
    listener = _prepare_socket(broker_socket)
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
    }

    def stop_service(_signum: int, _frame: Any) -> None:
        _SERVICE_STOPPING.set()
        with _ACTIVE_PROCESSES_LOCK:
            active = list(_ACTIVE_PROCESSES)
        for process in active:
            _terminate(process)
        try:
            listener.close()
        except OSError:
            pass

    for signum in previous_handlers:
        signal.signal(signum, stop_service)

    def handle_connection(connection: socket.socket) -> None:
        descriptors: list[int] = []
        with connection:
            connection.settimeout(SOCKET_TIMEOUT_SECONDS)
            try:
                request, descriptors = _recv_request(connection)
                _reject_pipelined_request(connection)
                response = _handle_request(
                    request,
                    descriptors=descriptors,
                    runtime=runtime,
                    connection=connection,
                )
            except Exception as exc:
                response = _error_response(exc)
            finally:
                for fd in descriptors:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            try:
                _send_frame(connection, response, limit=MAX_RESPONSE_BYTES)
            except (OSError, _ServerError):
                pass

    _SERVICE_STOPPING.clear()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CLIENT_WORKERS)
    try:
        while not _SERVICE_STOPPING.is_set():
            try:
                connection, _address = listener.accept()
            except OSError:
                if _SERVICE_STOPPING.is_set():
                    break
                raise
            try:
                if _peer_uid(connection) != os.getuid():
                    raise _ServerError("client belongs to another user", code="peer_authentication_failed")
                if not _CLIENT_SLOTS.acquire(blocking=False):
                    raise _ServerError("Capability Host is busy", code="broker_busy")
            except Exception as exc:
                try:
                    _send_frame(connection, _error_response(exc), limit=MAX_RESPONSE_BYTES)
                except (OSError, _ServerError):
                    pass
                connection.close()
                continue

            def admitted(conn: socket.socket = connection) -> None:
                try:
                    handle_connection(conn)
                finally:
                    _CLIENT_SLOTS.release()

            executor.submit(admitted)
    finally:
        _SERVICE_STOPPING.set()
        executor.shutdown(wait=False, cancel_futures=True)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        try:
            listener.close()
        except OSError:
            pass
        try:
            details = broker_socket.lstat()
            if stat.S_ISSOCK(details.st_mode) and details.st_uid == os.getuid():
                broker_socket.unlink()
        except OSError:
            pass


def server_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the signed RemCTL Capability Host")
    parser.add_argument("--socket", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        serve(broker_socket=args.socket.expanduser().absolute())
    except (OSError, _ServerError) as exc:
        print(f"RemCTL Capability Host failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return server_main(argv)


if __name__ == "__main__":
    raise SystemExit(server_main())
