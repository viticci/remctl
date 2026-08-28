#!/usr/bin/env python3
"""Deterministically build the RemCTL Capability Host runtime manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from remctl_host_manifest import build_runtime_manifest, manifest_digest, runtime_manifest_bytes


def build_manifest_payload(
    *,
    root: Path,
    protected_python: Path,
    broker_entrypoint: Path | None = None,
    host_version: str | None = None,
) -> dict[str, object]:
    return build_runtime_manifest(
        root=root,
        protected_python=protected_python,
        broker_entrypoint=broker_entrypoint,
        host_version=host_version,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, dest="protected_python")
    parser.add_argument("--output", required=True)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--broker-entrypoint")
    parser.add_argument("--host-version")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_manifest_payload(
        root=Path(args.root),
        protected_python=Path(args.protected_python),
        broker_entrypoint=Path(args.broker_entrypoint) if args.broker_entrypoint else None,
        host_version=args.host_version,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = runtime_manifest_bytes(payload)
    output_path.write_bytes(data)
    print(
        json.dumps(
            {
                "manifest": str(output_path.resolve(strict=False)),
                "manifestDigest": manifest_digest(payload),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
