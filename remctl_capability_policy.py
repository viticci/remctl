"""Command and resource policy for the signed RemCTL Capability Host.

This module contains data-only policy wherever practical so the client, the
broker, tests, and the sealed runtime all make the same routing decision.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from typing import Any

from remctl_runtime import (
    HOSTED_COMMANDS,
    LOCAL_COMMANDS,
    capability_host_command_scope,
)


PROTOCOL_VERSION = 2
SCOPE = "complete-protected-cli"

DESTRUCTIVE_COMMANDS = frozenset(
    {
        "delete",
        "group-delete",
        "list-delete",
        "section-delete",
        "smart-list-delete",
        "template-delete",
    }
)

IMAGE_COMMANDS = frozenset({"add", "edit"})
FILTER_FILE_COMMANDS = frozenset({"smart-list-create", "smart-list-edit"})

_REQUEST_SCHEMAS = {
    "ping": frozenset({"protocolVersion", "operation"}),
    "status": frozenset({"protocolVersion", "operation"}),
    "permissionStatus": frozenset({"protocolVersion", "operation"}),
    "requestPermission": frozenset({"protocolVersion", "operation", "permission"}),
    "run": frozenset(
        {
            "protocolVersion",
            "operation",
            "argv",
            "capabilities",
            "deadlineEpoch",
            "stdinBase64",
            "mergeOutput",
        }
    ),
}


class CapabilityPolicyError(ValueError):
    """The requested command is outside the fixed host policy."""


def _parser_commands(parser: argparse.ArgumentParser) -> frozenset[str]:
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(action, argparse._SubParsersAction) and isinstance(choices, dict):
            return frozenset(str(choice) for choice in choices)
    raise CapabilityPolicyError("RemCTL parser has no top-level command registry")


def validate_command_scope(parser: argparse.ArgumentParser) -> dict[str, list[str]]:
    """Require every real parser command to be classified exactly once."""

    parser_commands = _parser_commands(parser)
    overlap = LOCAL_COMMANDS & HOSTED_COMMANDS
    missing = parser_commands - LOCAL_COMMANDS - HOSTED_COMMANDS
    stale = (LOCAL_COMMANDS | HOSTED_COMMANDS) - parser_commands
    if overlap or missing or stale:
        details = []
        if overlap:
            details.append(f"overlap={sorted(overlap)!r}")
        if missing:
            details.append(f"missing={sorted(missing)!r}")
        if stale:
            details.append(f"stale={sorted(stale)!r}")
        raise CapabilityPolicyError(
            "RemCTL Capability Host command scope is incomplete: " + ", ".join(details)
        )
    return {
        "local": sorted(LOCAL_COMMANDS),
        "hosted": sorted(HOSTED_COMMANDS),
    }


def command_scope(command: str | None) -> str:
    """Return the policy scope for one already-parsed command."""

    try:
        return capability_host_command_scope(command)
    except ValueError as exc:
        raise CapabilityPolicyError(str(exc)) from exc


def namespace_is_hosted(namespace: Any) -> bool:
    return command_scope(getattr(namespace, "cmd", None)) == "hosted"


def validate_argv(
    argv: Iterable[str],
    *,
    parser: argparse.ArgumentParser,
) -> tuple[list[str], argparse.Namespace]:
    """Parse argv with the real CLI parser and enforce the hosted allowlist."""

    values = list(argv)
    if not values or len(values) > 128:
        raise CapabilityPolicyError("hosted argv must contain between 1 and 128 arguments")
    if any(not isinstance(value, str) or "\x00" in value for value in values):
        raise CapabilityPolicyError("hosted argv contains a non-string or NUL byte")
    if sum(len(value.encode("utf-8")) for value in values) > 48 * 1024:
        raise CapabilityPolicyError("hosted argv exceeds 48 KiB")
    try:
        namespace = parser.parse_args(values)
    except SystemExit as exc:
        raise CapabilityPolicyError("hosted argv does not parse") from exc
    validate_command_scope(parser)
    if not namespace_is_hosted(namespace):
        raise CapabilityPolicyError(
            f"command {getattr(namespace, 'cmd', None)!r} must run in the client"
        )
    return values, namespace


def request_schema(operation: str) -> frozenset[str]:
    """Return the exact allowed request fields for an IPC operation."""

    try:
        return _REQUEST_SCHEMAS[operation]
    except KeyError as exc:
        raise CapabilityPolicyError(f"unknown broker operation: {operation!r}") from exc
