"""Shared runtime helpers for RemCTL scripts."""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_STORE_SUBPATH = Path(
    "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores"
)
TRUTHY = {"1", "true", "yes", "on"}

CAPABILITY_HOST_MODE_ENV = "REMCTL_CAPABILITY_HOST"
CAPABILITY_HOST_ACTIVE_ENV = "REMCTL_CAPABILITY_HOST_ACTIVE"
CAPABILITY_HOST_MODES = frozenset({"auto", "force", "direct"})
LOCAL_COMMANDS = frozenset(
    {
        "completion",
        "doctor",
        "list-symbols",
        "mcp",
        "onboard",
        "permissions",
        "setup",
    }
)
HOSTED_COMMANDS = frozenset(
    {
        "add",
        "delete",
        "deleted",
        "restore",
        "done",
        "edit",
        "export",
        "flag",
        "flagged",
        "group-create",
        "group-delete",
        "group-edit",
        "group-info",
        "groups",
        "import",
        "info",
        "link",
        "list-create",
        "list-delete",
        "list-edit",
        "list-info",
        "list-pin",
        "list-rename",
        "list-unpin",
        "lists",
        "location-lookup",
        "open",
        "overdue",
        "reminder-move",
        "search",
        "section-create",
        "section-delete",
        "section-rename",
        "sections",
        "sharees",
        "show",
        "smart-list-create",
        "smart-list-delete",
        "smart-list-edit",
        "smart-lists",
        "stats",
        "subtasks",
        "tags",
        "template-apply",
        "template-create",
        "template-delete",
        "template-info",
        "templates",
        "today",
        "undone",
        "unflag",
        "upcoming",
        "urgent",
    }
)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY


def capability_host_mode(environ: dict[str, str] | None = None) -> str:
    """Return the requested execution mode after applying direct-only guards.

    The host's internal child process always uses direct execution. A custom
    Reminders store uses direct execution unless force mode requests the host,
    which is an explicit configuration conflict.
    """

    environment = os.environ if environ is None else environ
    if environment.get(CAPABILITY_HOST_ACTIVE_ENV) == "1":
        return "direct"
    raw_mode = environment.get(CAPABILITY_HOST_MODE_ENV, "auto").strip().lower()
    mode = raw_mode or "auto"
    if mode not in CAPABILITY_HOST_MODES:
        choices = ", ".join(sorted(CAPABILITY_HOST_MODES))
        raise ValueError(
            f"invalid {CAPABILITY_HOST_MODE_ENV} value {raw_mode!r}; expected {choices}"
        )
    if environment.get("REMCTL_STORE_DIR"):
        if mode == "force":
            raise ValueError(
                "REMCTL_CAPABILITY_HOST=force conflicts with REMCTL_STORE_DIR; "
                "custom stores can run only in auto or direct mode"
            )
        return "direct"
    return mode


def capability_host_command_scope(command: str | None) -> str:
    """Classify one parsed command as local-only or permission-bearing."""

    if command is None or command in HOSTED_COMMANDS:
        return "hosted"
    if command in LOCAL_COMMANDS:
        return "local"
    raise ValueError(f"unclassified RemCTL command: {command!r}")


def resolve_store_dir() -> Path:
    override = os.environ.get("REMCTL_STORE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_STORE_SUBPATH


def resolve_config_dir(app_name: str = "remctl") -> Path:
    override = os.environ.get("REMCTL_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    return base / app_name


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def write_private_text_file(path: Path, text: str) -> None:
    """Publish complete private state without exposing a truncated token/config."""
    ensure_private_dir(path.parent)
    if path.is_symlink():
        raise OSError(f"Refusing to overwrite a symbolic link: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def resolve_binary_path(script_path: str, binary_name: str, env_var: str) -> Path:
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()

    script_dir = Path(script_path).resolve().parent
    candidates = [
        script_dir / binary_name,
        script_dir / "bin" / binary_name,
        Path.home() / "bin" / binary_name,
        Path.home() / ".local" / "bin" / binary_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    discovered = shutil.which(binary_name)
    if discovered:
        return Path(discovered)

    return script_dir / binary_name


def start_of_day(now: datetime | None = None) -> datetime:
    current = now or datetime.now()
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


def due_today_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    sod = start_of_day(now)
    return sod, sod + timedelta(days=1)


def upcoming_window(days: int = 7, now: datetime | None = None) -> tuple[datetime, datetime]:
    sod = start_of_day(now)
    return sod, sod + timedelta(days=days + 1)


def mask_secret(secret: str, visible_chars: int = 4) -> str:
    if len(secret) <= visible_chars * 2:
        return "*" * len(secret)
    return f"{secret[:visible_chars]}...{secret[-visible_chars:]}"


def is_safe_remote_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.username or parsed.password:
        return False

    try:
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    if not hostname or hostname.rstrip(".").endswith((".local", ".localhost")) or hostname.rstrip(".") == "localhost":
        return False
    # TUN proxies use this range for DNS placeholders. Never allow it as an
    # explicit IP target, including legacy IPv4 spellings such as 0xc6120001.
    literal = False
    literal_host = hostname.rstrip(".")
    try:
        ipaddress.ip_address(literal_host)
        literal = True
    except ValueError:
        try:
            socket.inet_aton(literal_host)
            literal = True
        except OSError:
            pass
    try:
        addrinfo = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return False

    if not addrinfo:
        return False
    for _, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if not literal and ip.version == 4 and ip in ipaddress.ip_network("198.18.0.0/15"):
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def is_safe_terminal_text(text: str) -> bool:
    return not any(
        ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        for char in str(text)
    )
