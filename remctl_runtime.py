"""Shared runtime helpers for RemCTL scripts."""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_STORE_SUBPATH = Path(
    "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores"
)
TRUTHY = {"1", "true", "yes", "on"}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY


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
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    finally:
        try:
            path.chmod(0o600)
        except OSError:
            pass


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

    hostname = parsed.hostname
    if not hostname or hostname.endswith(".local"):
        return False

    try:
        addrinfo = socket.getaddrinfo(
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return False

    for _, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
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
