"""Read-only Unix-socket broker for the RemCTL Capability Host."""

from __future__ import annotations

# C5/C7: Under Python -I -S (the flags Swift uses to launch this script), the
# interpreter's isolated mode (-P, implied by -I) does NOT prepend the script's
# own directory to sys.path.  We add it here — before any relative imports — so
# all sibling modules (remctl_host_manifest, remctl_host_protocol, etc.) are
# importable.  The prepend is a no-op when the directory is already present.
import os as _os
import sys as _sys
_broker_dir = _os.path.dirname(_os.path.realpath(__file__))
if _broker_dir not in _sys.path:
    _sys.path.insert(0, _broker_dir)
del _broker_dir, _os, _sys

import json
import importlib.machinery
import importlib.util
import os
import socket
import stat
import struct
import sys
import threading
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from remctl_host_manifest import (
    RuntimeIdentityValidator,
    RuntimeIdentityValidatorProtocol,
    RuntimeManifestError,
)
from remctl_host_protocol import (
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    SCHEMA_MANIFEST_DIGEST,
    SCHEMA_MANIFEST_VERSION,
    IMPLEMENTED_OPERATIONS,
    ProtocolError,
    decode_request,
)

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
FRAME_HEADER_BYTES = 4
DEFAULT_WORKERS = 4
DEFAULT_QUEUE_LIMIT = 16
DEFAULT_CONNECTION_TIMEOUT = 5.0


class BrokerSecurityError(RuntimeError):
    pass


def _require_private_directory(path: Path) -> None:
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise BrokerSecurityError(f"socket parent is not a directory: {path}")
    if metadata.st_uid != os.getuid():
        raise BrokerSecurityError(f"socket parent is not owned by current user: {path}")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise BrokerSecurityError(f"socket parent must have mode 0700: {path}")


def prepare_socket_path(path: Path) -> None:
    """Create a private parent and remove only a safe stale socket."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    _require_private_directory(path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise BrokerSecurityError(f"refusing symlink at socket path: {path}")
    if metadata.st_uid != os.getuid():
        raise BrokerSecurityError(f"socket path is not owned by current user: {path}")
    if not stat.S_ISSOCK(metadata.st_mode):
        raise BrokerSecurityError(f"refusing non-socket occupant: {path}")
    path.unlink()


def bind_server_socket(path: Path, backlog: int = 16) -> socket.socket:
    prepare_socket_path(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        path.chmod(0o600)
        metadata = path.stat()
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise BrokerSecurityError("created socket has unsafe ownership or mode")
        server.listen(backlog)
        return server
    except Exception:
        server.close()
        try:
            metadata = path.lstat()
            if metadata.st_uid == os.getuid() and stat.S_ISSOCK(metadata.st_mode):
                path.unlink()
        except FileNotFoundError:
            pass
        raise


def encode_frame(payload: dict[str, Any], maximum: int = MAX_RESPONSE_BYTES) -> bytes:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise ProtocolError("response_too_large", "response exceeds byte limit")
    return struct.pack(">I", len(encoded)) + encoded


def decode_frame(data: bytes, maximum: int = MAX_REQUEST_BYTES) -> bytes:
    if len(data) < FRAME_HEADER_BYTES:
        raise ProtocolError("invalid_frame", "frame header is incomplete")
    length = struct.unpack(">I", data[:FRAME_HEADER_BYTES])[0]
    if length > maximum:
        raise ProtocolError("request_too_large", "request exceeds byte limit")
    body = data[FRAME_HEADER_BYTES:]
    if len(body) != length:
        raise ProtocolError("invalid_frame", "frame length does not match payload")
    return body


class ReadBroker:
    """Dispatches only explicitly registered read-only operations."""

    def __init__(
        self,
        handlers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
        *,
        identity_validator: RuntimeIdentityValidatorProtocol,
    ):
        if identity_validator is None:
            raise ValueError("identity_validator is required")
        self._identity_validator = identity_validator
        self._handlers = {"health": self._health}
        if handlers:
            overlap = set(handlers).intersection(self._handlers)
            if overlap:
                raise ValueError(f"cannot replace built-in handlers: {sorted(overlap)}")
            self._handlers.update(handlers)
        if set(self._handlers) != set(IMPLEMENTED_OPERATIONS):
            raise ValueError(
                "registered handlers must exactly match the schema manifest's "
                "implemented operations"
            )

    @staticmethod
    def _health(
        identity, _request: dict[str, Any]
    ) -> dict[str, Any]:
        return identity.health_payload(IMPLEMENTED_OPERATIONS)

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            identity = self._identity_validator.validate()
        except RuntimeManifestError as exc:
            raise ProtocolError(
                "host_identity_invalid",
                "Capability Host runtime identity validation failed",
            ) from exc
        operation = request["operation"]
        handler = self._handlers.get(operation)
        if handler is None:
            raise ProtocolError(
                "operation_unavailable",
                "operation is valid but unavailable in this host build",
            )
        if operation == "health":
            result = self._health(identity, request)
        else:
            try:
                result = handler(request)
            except ProtocolError:
                raise
            except BaseException as exc:
                # C3: catch any unexpected exception (including SystemExit
                # from inner helpers) at the dispatch boundary so the broker
                # process survives and can serve the next request.
                raise ProtocolError(
                    "internal_error",
                    "Capability Host handler raised an unexpected error",
                ) from exc
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
            "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
            "requestId": request["requestId"],
            "status": "ok",
            "result": result,
            "warnings": [],
        }

    def handle_frame(self, frame: bytes) -> bytes:
        request_id = None
        try:
            body = decode_frame(frame)
            request = decode_request(body)
            request_id = request["requestId"]
            response = self.dispatch(request)
        except ProtocolError as exc:
            response = {
                "protocolVersion": PROTOCOL_VERSION,
                "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
                "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
                "requestId": request_id,
                **exc.to_payload(),
            }
        return encode_frame(response)


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = conn.recv(remaining)
        if not chunk:
            raise ProtocolError("invalid_frame", "connection closed before frame completed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_frame(conn: socket.socket) -> bytes:
    header = _recv_exact(conn, FRAME_HEADER_BYTES)
    length = struct.unpack(">I", header)[0]
    if length > MAX_REQUEST_BYTES:
        raise ProtocolError("request_too_large", "request exceeds byte limit")
    return header + _recv_exact(conn, length)


def serve_connection(conn: socket.socket, read_broker: ReadBroker) -> None:
    with conn:
        conn.settimeout(DEFAULT_CONNECTION_TIMEOUT)
        if hasattr(conn, "getpeereid"):
            peer_uid, _ = conn.getpeereid()
            if peer_uid != os.getuid():
                raise BrokerSecurityError("refusing connection from another user")
        try:
            response = read_broker.handle_frame(receive_frame(conn))
        except socket.timeout:
            response = encode_frame(
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
                    "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
                    "requestId": None,
                    "status": "error",
                    "code": "request_timeout",
                    "message": "Capability Host request timed out",
                }
            )
        except ProtocolError as exc:
            response = encode_frame(
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
                    "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
                    "requestId": None,
                    **exc.to_payload(),
                }
            )
        conn.sendall(response)


class BrokerServer:
    def __init__(
        self,
        socket_path: Path,
        read_broker: ReadBroker | None,
        *,
        workers: int = DEFAULT_WORKERS,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
    ):
        if workers < 1 or queue_limit < workers:
            raise ValueError("queue_limit must be at least the worker count")
        if read_broker is None:
            raise ValueError("read_broker is required")
        self.socket_path = Path(socket_path)
        self.read_broker = read_broker
        self.workers = workers
        self._slots = threading.BoundedSemaphore(queue_limit)
        self._stopping = threading.Event()
        self._server: socket.socket | None = None

    def stop(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.close()

    def serve_forever(self) -> None:
        self._server = bind_server_socket(self.socket_path)
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                while not self._stopping.is_set():
                    try:
                        conn, _ = self._server.accept()
                    except OSError:
                        if self._stopping.is_set():
                            break
                        raise
                    if not self._slots.acquire(blocking=False):
                        with conn:
                            conn.sendall(
                                encode_frame(
                                    {
                                        "protocolVersion": PROTOCOL_VERSION,
                                        "schemaManifestVersion": SCHEMA_MANIFEST_VERSION,
                                        "schemaManifestDigest": SCHEMA_MANIFEST_DIGEST,
                                        "requestId": None,
                                        "status": "error",
                                        "code": "server_busy",
                                        "message": "Capability Host queue is full",
                                    }
                                )
                            )
                        continue

                    def run(client: socket.socket) -> None:
                        try:
                            serve_connection(client, self.read_broker)
                        finally:
                            self._slots.release()

                    executor.submit(run, conn)
        finally:
            self._server.close()
            self._server = None
            try:
                metadata = self.socket_path.lstat()
                if metadata.st_uid == os.getuid() and stat.S_ISSOCK(metadata.st_mode):
                    self.socket_path.unlink()
            except FileNotFoundError:
                pass


def _load_remctl_module(validated_identity=None):
    """Load the remctl CLI module.

    When *validated_identity* is provided (the result of a successful manifest
    validation), the module is loaded from the exact path declared in the
    manifest's ``runtimeFiles["remctl"]`` entry — not from a sibling
    discovered by convention.  This is the B1 security requirement: the
    broker must execute the file whose hash was validated, not whatever file
    happens to sit next to the broker script.

    The ``remctl_hosted_cli`` escape hatch is retained for tests that pre-
    install a substitute module under that name.
    """
    try:
        import remctl_hosted_cli
    except ImportError:
        remctl_hosted_cli = None
    if remctl_hosted_cli is not None:
        return remctl_hosted_cli

    if validated_identity is not None:
        remctl_file = next(
            (item for item in validated_identity.runtime_files if item.key == "remctl"),
            None,
        )
        if remctl_file is None:
            raise BrokerSecurityError(
                "validated manifest has no 'remctl' runtime file key"
            )
        module_path = Path(remctl_file.path)
    else:
        module_path = Path(__file__).resolve().with_name("remctl")

    metadata = module_path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BrokerSecurityError("remctl module path must be a regular non-symlink file")
    loader = importlib.machinery.SourceFileLoader("remctl_hosted_cli", str(module_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise BrokerSecurityError("unable to create remctl module specification")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-digest", required=True)
    args = parser.parse_args(argv)
    socket_path = Path(args.socket)
    if not socket_path.is_absolute() or "\n" in str(socket_path) or "\r" in str(socket_path):
        parser.error("--socket must be a safe absolute path")
    manifest_path = Path(args.manifest)
    if (
        not manifest_path.is_absolute()
        or "\n" in str(manifest_path)
        or "\r" in str(manifest_path)
    ):
        parser.error("--manifest must be a safe absolute path")
    if (
        len(args.manifest_digest) != 64
        or any(char not in "0123456789abcdef" for char in args.manifest_digest)
    ):
        parser.error("--manifest-digest must be 64 lowercase hexadecimal characters")
    identity_validator = RuntimeIdentityValidator(
        manifest_path,
        args.manifest_digest,
    )
    # Phase 1 (N6): validate the manifest before executing any runtime code
    # declared by it.  validate() raises RuntimeManifestError on any identity
    # or hash mismatch.  The return value is passed to _load_remctl_module so
    # the broker loads the exact manifest-declared remctl path (B1), never a
    # sibling by convention.  Phase 2 re-validation happens per-dispatch inside
    # ReadBroker.dispatch() using the same identity_validator.
    validated_identity = identity_validator.validate()
    from remctl_host_operations import ReadOperations

    handlers = ReadOperations(_load_remctl_module(validated_identity)).handlers()
    BrokerServer(
        socket_path,
        ReadBroker(handlers, identity_validator=identity_validator),
    ).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
