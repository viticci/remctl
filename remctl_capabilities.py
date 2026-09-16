"""Descriptor-backed file capabilities for RemCTL Capability Host requests."""

from __future__ import annotations

import base64
from collections import Counter
import fcntl
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from remctl_capability_policy import (
    DESTRUCTIVE_COMMANDS,
    FILTER_FILE_COMMANDS,
    IMAGE_COMMANDS,
)


CAPABILITY_PREFIX = "remctl-capability:"
CAPABILITY_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
CAPABILITY_REFERENCE_PATTERN = re.compile(
    re.escape(CAPABILITY_PREFIX) + r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
)
MAX_CAPABILITIES = 64
MAX_INPUT_FILE_BYTES = 128 * 1024 * 1024
MAX_STDIN_BYTES = 8 * 1024 * 1024
MIN_TERMINAL_COLUMNS = 20
MAX_TERMINAL_COLUMNS = 1000


class CapabilityError(ValueError):
    """A caller-supplied descriptor or path violates the host policy."""


@dataclass
class DescriptorCapability:
    identifier: str
    fd: int
    kind: str
    purpose: str
    device: int
    inode: int
    mode: int
    size: int
    name: str | None = None
    columns: int | None = None

    def metadata(self, index: int) -> dict[str, Any]:
        return {
            "id": self.identifier,
            "fdIndex": index,
            "kind": self.kind,
            "purpose": self.purpose,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "size": self.size,
            "name": self.name,
            "columns": self.columns,
        }


@dataclass
class CapabilityBundle:
    argv: list[str]
    descriptors: list[DescriptorCapability] = field(default_factory=list)
    stdin_bytes: bytes = b""

    @property
    def fds(self) -> list[int]:
        return [item.fd for item in self.descriptors]

    @property
    def metadata(self) -> list[dict[str, Any]]:
        return [item.metadata(index) for index, item in enumerate(self.descriptors)]

    @property
    def stdin_base64(self) -> str:
        return base64.b64encode(self.stdin_bytes).decode("ascii")

    def close(self) -> None:
        for item in self.descriptors:
            try:
                os.close(item.fd)
            except OSError:
                pass
        self.descriptors.clear()

    def __enter__(self) -> "CapabilityBundle":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _open_input(path_value: str, *, purpose: str, identifier: str) -> DescriptorCapability:
    path = Path(path_value).expanduser()
    try:
        before = path.lstat()
    except OSError as exc:
        raise CapabilityError(f"cannot inspect {purpose} input") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CapabilityError(f"{purpose} input must be a regular non-symlink file")
    if before.st_size > MAX_INPUT_FILE_BYTES:
        raise CapabilityError(f"{purpose} input exceeds 128 MiB")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CapabilityError(f"cannot open {purpose} input") from exc
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode):
            raise CapabilityError(f"{purpose} input changed type while opening")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise CapabilityError(f"{purpose} input changed while opening")
        if after.st_size > MAX_INPUT_FILE_BYTES:
            raise CapabilityError(f"{purpose} input exceeds 128 MiB")
        access_mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
        if access_mode != os.O_RDONLY:
            raise CapabilityError(f"{purpose} input must be opened read-only")
        raw_name = path.name
        safe_name = "".join(
            "_" if ord(character) < 0x20 or character in {"/", "\\", ":"} else character
            for character in raw_name
        ).strip(". ")
        while len(safe_name.encode("utf-8")) > 160:
            safe_name = safe_name[:-1]
        if not safe_name or safe_name in {".", ".."}:
            safe_name = "input"
        return DescriptorCapability(
            identifier=identifier,
            fd=fd,
            kind="input-file",
            purpose=purpose,
            device=after.st_dev,
            inode=after.st_ino,
            mode=stat.S_IFMT(after.st_mode) | stat.S_IMODE(after.st_mode),
            size=after.st_size,
            name=safe_name,
        )
    except Exception:
        os.close(fd)
        raise


def _open_tty(
    stream: Any,
    *,
    identifier: str,
    purpose: str,
) -> DescriptorCapability | None:
    fd: int | None = None
    try:
        if not stream.isatty():
            return None
        original = stream.fileno()
        fd = os.dup(original)
        details = os.fstat(fd)
        if not stat.S_ISCHR(details.st_mode) or not os.isatty(fd):
            raise CapabilityError("interactive stdin is not a terminal character device")
        access_mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
        allowed_modes = (
            {os.O_RDONLY, os.O_RDWR}
            if purpose == "stdin"
            else {os.O_WRONLY, os.O_RDWR}
        )
        if access_mode not in allowed_modes:
            raise CapabilityError(f"{purpose} terminal has an incompatible access mode")
        try:
            columns = os.get_terminal_size(fd).columns
        except OSError:
            columns = None
        if columns is not None and not MIN_TERMINAL_COLUMNS <= columns <= MAX_TERMINAL_COLUMNS:
            columns = None
        capability = DescriptorCapability(
            identifier=identifier,
            fd=fd,
            kind="tty",
            purpose=purpose,
            device=details.st_dev,
            inode=details.st_ino,
            mode=stat.S_IFMT(details.st_mode) | stat.S_IMODE(details.st_mode),
            size=0,
            columns=columns,
        )
        fd = None  # The returned capability now owns the duplicate.
        return capability
    except (AttributeError, OSError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class _Planner:
    def __init__(self, argv: list[str]) -> None:
        self.argv = list(argv)
        self.descriptors: list[DescriptorCapability] = []
        try:
            self._option_scan_end = self.argv.index("--")
        except ValueError:
            self._option_scan_end = len(self.argv)

    def add_input(self, raw_path: str, purpose: str) -> str:
        if len(self.descriptors) >= MAX_CAPABILITIES:
            raise CapabilityError("request has more than 64 descriptor capabilities")
        identifier = f"input-{len(self.descriptors)}"
        capability = _open_input(raw_path, purpose=purpose, identifier=identifier)
        self.descriptors.append(capability)
        return f"{CAPABILITY_PREFIX}{identifier}"

    def rewrite_option(self, option: str, purpose: str) -> None:
        index = 0
        while index < self._option_scan_end:
            token = self.argv[index]
            if token == option:
                if index + 1 >= self._option_scan_end:
                    raise CapabilityError(f"{option} is missing its file value")
                self.argv[index + 1] = self.add_input(self.argv[index + 1], purpose)
                index += 2
                continue
            prefix = option + "="
            if token.startswith(prefix):
                self.argv[index] = prefix + self.add_input(token[len(prefix) :], purpose)
            index += 1

    def rewrite_filter(self) -> None:
        index = 0
        while index < self._option_scan_end:
            token = self.argv[index]
            value_index: int | None = None
            prefix = "--filter-json="
            if token == "--filter-json":
                value_index = index + 1
                if value_index >= self._option_scan_end:
                    raise CapabilityError("--filter-json is missing its value")
                value = self.argv[value_index]
            elif token.startswith(prefix):
                value = token[len(prefix) :]
            else:
                index += 1
                continue
            if value.startswith("@"):
                rewritten = "@" + self.add_input(value[1:], "filter-json")
                if value_index is None:
                    self.argv[index] = prefix + rewritten
                else:
                    self.argv[value_index] = rewritten
            index += 2 if value_index is not None else 1

    def rewrite_subtasks(self) -> None:
        index = 0
        while index < self._option_scan_end:
            token = self.argv[index]
            value_index: int | None = None
            prefix = "--subtask="
            if token == "--subtask":
                value_index = index + 1
                if value_index >= self._option_scan_end:
                    raise CapabilityError("--subtask is missing its value")
                value = self.argv[value_index]
            elif token.startswith(prefix):
                value = token[len(prefix) :]
            else:
                index += 1
                continue
            if not value.lstrip().startswith("{"):
                index += 2 if value_index is not None else 1
                continue
            try:
                payload = json.loads(value)
            except json.JSONDecodeError as exc:
                raise CapabilityError("invalid --subtask JSON") from exc
            if not isinstance(payload, dict):
                raise CapabilityError("--subtask JSON must be an object")
            key = "images" if "images" in payload else ("image" if "image" in payload else None)
            if key is not None:
                images = payload[key]
                if isinstance(images, str):
                    payload[key] = self.add_input(images, "subtask-image")
                elif isinstance(images, list) and all(isinstance(item, str) for item in images):
                    payload[key] = [self.add_input(item, "subtask-image") for item in images]
                else:
                    raise CapabilityError("subtask images must be a string or string array")
            rewritten = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if value_index is None:
                self.argv[index] = prefix + rewritten
            else:
                self.argv[value_index] = rewritten
            index += 2 if value_index is not None else 1

    def rewrite_import(self, raw_path: str) -> None:
        try:
            command_index = self.argv.index("import")
        except ValueError as exc:
            raise CapabilityError("parsed import command is absent from argv") from exc
        for index in range(command_index + 1, len(self.argv)):
            if self.argv[index] == raw_path:
                self.argv[index] = self.add_input(raw_path, "import")
                return
        raise CapabilityError("could not bind the import file argument")


def plan_invocation(
    argv: list[str],
    parsed_args: Any,
    *,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
) -> CapabilityBundle:
    """Open caller files and rewrite argv to opaque capability identifiers."""

    planner = _Planner(argv)
    try:
        command = getattr(parsed_args, "cmd", None)
        import_from_stdin = command == "import" and getattr(parsed_args, "file", None) == "-"
        if command == "import" and not import_from_stdin:
            planner.rewrite_import(str(getattr(parsed_args, "file")))
        if command in IMAGE_COMMANDS:
            planner.rewrite_option("--image", "image")
            planner.rewrite_subtasks()
        if command in FILTER_FILE_COMMANDS:
            planner.rewrite_filter()

        stdin_bytes = b""
        if import_from_stdin:
            import sys

            input_stream = sys.stdin if stdin is None else stdin
            if input_stream.isatty():
                raise CapabilityError("import - requires piped standard input")
            binary_stream = getattr(input_stream, "buffer", input_stream)
            stdin_bytes = binary_stream.read(MAX_STDIN_BYTES + 1)
            if isinstance(stdin_bytes, str):
                stdin_bytes = stdin_bytes.encode("utf-8")
            if len(stdin_bytes) > MAX_STDIN_BYTES:
                raise CapabilityError("standard input exceeds 8 MiB")
        interactive_confirmation = bool(
            command in DESTRUCTIVE_COMMANDS
            and not getattr(parsed_args, "force", False)
            and not getattr(parsed_args, "json", False)
        )
        if interactive_confirmation:
            import sys

            input_stream = sys.stdin if stdin is None else stdin
            tty = _open_tty(
                input_stream,
                identifier=f"tty-{len(planner.descriptors)}",
                purpose="stdin",
            )
            if tty is not None:
                if len(planner.descriptors) >= MAX_CAPABILITIES:
                    os.close(tty.fd)
                    raise CapabilityError("request has more than 64 descriptor capabilities")
                planner.descriptors.append(tty)

                error_stream = sys.stderr if stderr is None else stderr
                error_tty = _open_tty(
                    error_stream,
                    identifier=f"tty-{len(planner.descriptors)}",
                    purpose="stderr",
                )
                if error_tty is None:
                    raise CapabilityError(
                        "interactive hosted confirmation requires terminal stderr; use --force"
                    )
                if len(planner.descriptors) >= MAX_CAPABILITIES:
                    os.close(error_tty.fd)
                    raise CapabilityError("request has more than 64 descriptor capabilities")
                planner.descriptors.append(error_tty)

        output_stream = stdout
        if output_stream is None:
            import sys

            output_stream = sys.stdout
        output_tty = _open_tty(
            output_stream,
            identifier=f"tty-{len(planner.descriptors)}",
            purpose="stdout",
        )
        if output_tty is not None:
            if len(planner.descriptors) >= MAX_CAPABILITIES:
                os.close(output_tty.fd)
                raise CapabilityError("request has more than 64 descriptor capabilities")
            planner.descriptors.append(output_tty)
        return CapabilityBundle(planner.argv, planner.descriptors, stdin_bytes)
    except Exception:
        for descriptor in planner.descriptors:
            try:
                os.close(descriptor.fd)
            except OSError:
                pass
        raise


def validate_received_capabilities(
    metadata: Any,
    descriptors: list[int],
) -> dict[str, DescriptorCapability]:
    """Bind received SCM_RIGHTS descriptors to exact, untrusted metadata."""

    if not isinstance(metadata, list) or len(metadata) != len(descriptors):
        raise CapabilityError("capability metadata does not match received descriptors")
    if len(descriptors) > MAX_CAPABILITIES:
        raise CapabilityError("request has more than 64 descriptor capabilities")
    result: dict[str, DescriptorCapability] = {}
    allowed_keys = {
        "id",
        "fdIndex",
        "kind",
        "purpose",
        "device",
        "inode",
        "mode",
        "size",
        "name",
        "columns",
    }
    for expected_index, (item, fd) in enumerate(zip(metadata, descriptors, strict=True)):
        if not isinstance(item, dict) or set(item) != allowed_keys:
            raise CapabilityError("capability metadata is malformed")
        if item.get("fdIndex") != expected_index:
            raise CapabilityError("capability descriptor order is invalid")
        identifier = item.get("id")
        kind = item.get("kind")
        purpose = item.get("purpose")
        if (
            not isinstance(identifier, str)
            or not identifier
            or CAPABILITY_IDENTIFIER_PATTERN.fullmatch(identifier) is None
            or identifier in result
            or kind not in {"input-file", "tty"}
            or not isinstance(purpose, str)
        ):
            raise CapabilityError("capability identity is invalid")
        details = os.fstat(fd)
        if (details.st_dev, details.st_ino) != (item.get("device"), item.get("inode")):
            raise CapabilityError("received descriptor identity does not match metadata")
        expected_mode = stat.S_IFMT(details.st_mode) | stat.S_IMODE(details.st_mode)
        if expected_mode != item.get("mode"):
            raise CapabilityError("received descriptor mode does not match metadata")
        if kind == "input-file":
            if not stat.S_ISREG(details.st_mode) or details.st_size != item.get("size"):
                raise CapabilityError("input descriptor is not the planned regular file")
            name = item.get("name")
            if (
                not isinstance(name, str)
                or not name
                or name in {".", ".."}
                or Path(name).name != name
                or any(ord(character) < 0x20 or character in {"/", "\\", ":"} for character in name)
                or len(name.encode("utf-8")) > 160
            ):
                raise CapabilityError("input descriptor filename is invalid")
            access_mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
            if access_mode != os.O_RDONLY:
                raise CapabilityError("input descriptor must be opened read-only")
        else:
            if (
                purpose not in {"stdin", "stdout", "stderr"}
                or item.get("name") is not None
                or not stat.S_ISCHR(details.st_mode)
                or not os.isatty(fd)
            ):
                raise CapabilityError("TTY descriptor is not an interactive terminal")
            access_mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
            allowed_modes = (
                {os.O_RDONLY, os.O_RDWR}
                if purpose == "stdin"
                else {os.O_WRONLY, os.O_RDWR}
            )
            if access_mode not in allowed_modes:
                raise CapabilityError("TTY descriptor access mode does not match its purpose")
            columns = item.get("columns")
            if columns is not None and (
                not isinstance(columns, int)
                or isinstance(columns, bool)
                or not MIN_TERMINAL_COLUMNS <= columns <= MAX_TERMINAL_COLUMNS
            ):
                raise CapabilityError("TTY column metadata is invalid")
            try:
                actual_columns = os.get_terminal_size(fd).columns
            except OSError:
                actual_columns = None
            if columns is not None and actual_columns is not None and columns != actual_columns:
                raise CapabilityError("TTY column metadata does not match the terminal")
        os.set_inheritable(fd, False)
        result[identifier] = DescriptorCapability(
            identifier=identifier,
            fd=fd,
            kind=kind,
            purpose=purpose,
            device=details.st_dev,
            inode=details.st_ino,
            mode=expected_mode,
            size=details.st_size if kind == "input-file" else 0,
            name=item.get("name") if kind == "input-file" else None,
            columns=actual_columns if kind == "tty" else None,
        )
    return result


def validate_capability_bindings(
    argv: list[str],
    parsed_args: Any,
    capabilities: dict[str, DescriptorCapability],
) -> None:
    """Require every protected file field to name one exact-purpose capability."""

    expected: list[tuple[str, str]] = []

    def require_reference(value: Any, purpose: str, field: str) -> None:
        if not isinstance(value, str) or not value.startswith(CAPABILITY_PREFIX):
            raise CapabilityError(f"{field} must use a descriptor-backed capability")
        identifier = value[len(CAPABILITY_PREFIX) :]
        if CAPABILITY_IDENTIFIER_PATTERN.fullmatch(identifier) is None:
            raise CapabilityError(f"{field} capability identifier is invalid")
        expected.append((identifier, purpose))

    command = getattr(parsed_args, "cmd", None)
    if command == "import" and getattr(parsed_args, "file", None) != "-":
        require_reference(getattr(parsed_args, "file", None), "import", "import file")

    if command in IMAGE_COMMANDS:
        for image in getattr(parsed_args, "image", None) or []:
            require_reference(image, "image", "--image")
        for subtask in getattr(parsed_args, "subtask", None) or []:
            if not isinstance(subtask, str) or not subtask.lstrip().startswith("{"):
                continue
            try:
                payload = json.loads(subtask)
            except json.JSONDecodeError as exc:
                raise CapabilityError("invalid --subtask JSON") from exc
            if not isinstance(payload, dict):
                raise CapabilityError("--subtask JSON must be an object")
            key = "images" if "images" in payload else ("image" if "image" in payload else None)
            if key is None:
                continue
            values = payload[key]
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise CapabilityError("subtask images must be a string or string array")
            for value in values:
                require_reference(value, "subtask-image", f"--subtask {key}")

    if command in FILTER_FILE_COMMANDS:
        filter_json = getattr(parsed_args, "filter_json", None)
        if isinstance(filter_json, str) and filter_json.startswith("@"):
            require_reference(filter_json[1:], "filter-json", "--filter-json @file")

    input_capabilities = {
        identifier: item
        for identifier, item in capabilities.items()
        if item.kind == "input-file"
    }
    expected_ids = [identifier for identifier, _purpose in expected]
    if len(expected_ids) != len(set(expected_ids)):
        raise CapabilityError("an input capability is referenced more than once")
    if set(expected_ids) != set(input_capabilities):
        raise CapabilityError("input capabilities do not exactly match protected file arguments")
    for identifier, purpose in expected:
        if input_capabilities[identifier].purpose != purpose:
            raise CapabilityError("input capability purpose does not match its argument")

    tty_by_purpose: dict[str, list[DescriptorCapability]] = {}
    for item in capabilities.values():
        if item.kind == "tty":
            tty_by_purpose.setdefault(item.purpose, []).append(item)
    if any(len(items) != 1 for items in tty_by_purpose.values()):
        raise CapabilityError("request has duplicate TTY capabilities")
    if set(tty_by_purpose) - {"stdin", "stdout", "stderr"}:
        raise CapabilityError("request has an unsupported TTY capability")
    interactive = bool(
        command in DESTRUCTIVE_COMMANDS
        and not getattr(parsed_args, "force", False)
        and not getattr(parsed_args, "json", False)
    )
    if "stdin" in tty_by_purpose or "stderr" in tty_by_purpose:
        if not interactive or set(tty_by_purpose) & {"stdin", "stderr"} != {"stdin", "stderr"}:
            raise CapabilityError("stdin and stderr TTYs are allowed only as an interactive pair")

    actual_references = Counter(
        match.group(0)
        for value in argv
        for match in CAPABILITY_REFERENCE_PATTERN.finditer(value)
    )
    expected_references = Counter(
        f"{CAPABILITY_PREFIX}{identifier}" for identifier in input_capabilities
    )
    if actual_references != expected_references:
        raise CapabilityError("capability identifier occurrence count is invalid")


def materialize_inputs(
    argv: list[str],
    capabilities: dict[str, DescriptorCapability],
    directory: Path,
) -> tuple[list[str], int | None, int | None, int | None]:
    """Copy descriptor inputs into private host staging and rewrite opaque IDs."""

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    replacements: dict[str, str] = {}
    tty_fd: int | None = None
    stdout_columns: int | None = None
    stderr_fd: int | None = None
    for index, item in enumerate(capabilities.values()):
        if item.kind == "tty":
            if item.purpose == "stdin":
                if tty_fd is not None:
                    raise CapabilityError("request has more than one stdin TTY")
                tty_fd = item.fd
            elif item.purpose == "stdout":
                if stdout_columns is not None:
                    raise CapabilityError("request has more than one stdout TTY")
                stdout_columns = item.columns or 80
            elif item.purpose == "stderr":
                if stderr_fd is not None:
                    raise CapabilityError("request has more than one stderr TTY")
                stderr_fd = item.fd
            continue
        destination = directory / f"input-{index:03d}-{item.name or 'input'}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        output_fd = os.open(destination, flags, 0o600)
        copied = 0
        try:
            os.lseek(item.fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(item.fd, min(1024 * 1024, MAX_INPUT_FILE_BYTES + 1 - copied))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_INPUT_FILE_BYTES:
                    raise CapabilityError("input descriptor exceeds 128 MiB while staging")
                view = memoryview(chunk)
                while view:
                    written = os.write(output_fd, view)
                    view = view[written:]
        finally:
            os.close(output_fd)
        replacements[f"{CAPABILITY_PREFIX}{item.identifier}"] = str(destination)

    used: dict[str, int] = {opaque: 0 for opaque in replacements}

    def replace_exact(value: str) -> str:
        destination = replacements.get(value)
        if destination is None:
            return value
        used[value] += 1
        return destination

    def rewrite_subtask(value: str) -> str:
        if not value.lstrip().startswith("{"):
            return value
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return value
        if not isinstance(payload, dict):
            return value
        key = "images" if "images" in payload else ("image" if "image" in payload else None)
        if key is None:
            return value
        images = payload[key]
        if isinstance(images, str):
            payload[key] = replace_exact(images)
        elif isinstance(images, list):
            payload[key] = [replace_exact(item) if isinstance(item, str) else item for item in images]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    rewritten = list(argv)
    command = next((value for value in argv if value in {"import", *IMAGE_COMMANDS, *FILTER_FILE_COMMANDS}), None)
    index = 0
    while index < len(rewritten):
        token = rewritten[index]
        if command == "import" and token.startswith(CAPABILITY_PREFIX):
            rewritten[index] = replace_exact(token)
        elif command in IMAGE_COMMANDS:
            if token == "--image" and index + 1 < len(rewritten):
                rewritten[index + 1] = replace_exact(rewritten[index + 1])
                index += 1
            elif token.startswith("--image="):
                rewritten[index] = "--image=" + replace_exact(token[len("--image=") :])
            elif token == "--subtask" and index + 1 < len(rewritten):
                rewritten[index + 1] = rewrite_subtask(rewritten[index + 1])
                index += 1
            elif token.startswith("--subtask="):
                rewritten[index] = "--subtask=" + rewrite_subtask(token[len("--subtask=") :])
        if command in FILTER_FILE_COMMANDS:
            if token == "--filter-json" and index + 1 < len(rewritten):
                value = rewritten[index + 1]
                if value.startswith("@"):
                    rewritten[index + 1] = "@" + replace_exact(value[1:])
                index += 1
            elif token.startswith("--filter-json=@"):
                rewritten[index] = "--filter-json=@" + replace_exact(
                    token[len("--filter-json=@") :]
                )
        index += 1

    if any(count != 1 for count in used.values()):
        raise CapabilityError("input capability was not materialized exactly once")
    return rewritten, tty_fd, stdout_columns, stderr_fd
