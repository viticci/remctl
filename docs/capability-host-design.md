# Capability Host Design

## Purpose

macOS may deny a terminal, automation agent, or Python process direct access
to the Reminders SQLite store unless that caller has Full Disk Access (FDA).
Granting FDA to every possible caller creates a broad and unstable permission
boundary.

RemCTL confines FDA to one background-only application:

```text
unprivileged remctl
        |
        | closed, typed, read-only RPC
        v
RemCTL Capability Host.app (FDA)
        |
        v
Reminders SQLite store
```

The host supplies database reads only. EventKit, AppleScript,
`remctl-bridge`, and `remctl-private` remain unprivileged and perform all
mutations.

## Security Invariants

- The host exposes a fixed set of typed read operations.
- Raw command-line arguments, SQL, paths, environment variables, executable
  selection, and mutation verbs never cross the socket.
- The host does not invoke EventKit, AppleScript, subprocesses, the Swift
  bridge, or the private mutation helper.
- The socket directory is owned by the current user with mode `0700`.
- The socket is owned by the current user with mode `0600`.
- Symlinked socket paths and unexpected peer identities are rejected.
- Requests and responses use bounded length framing, schema validation,
  deadlines, response limits, and bounded concurrency.
- The protected runtime is sealed by a manifest and revalidated before each
  request dispatch.
- The exact manifest-validated CLI module is loaded; import-path substitution
  is not permitted.

This is a same-user trust boundary, not a process sandbox. Another process
running as the same macOS user may connect only if it can satisfy the socket
and protocol checks, and it can request only the closed read operations.

## Components

| Component | Responsibility |
| --- | --- |
| `remctl-capability-host.swift` | Background app launcher, bundle/runtime validation, broker launch |
| `remctl_read_broker.py` | Unix-socket server, framing, peer checks, dispatch confinement |
| `remctl_host_protocol.py` | Versioned operation schemas, bounds, validation, protocol digest |
| `remctl_host_operations.py` | Read-only typed snapshots over the Reminders store |
| `remctl_host_manifest.py` | Runtime identity construction and validation |
| `remctl_host.py` | Unprivileged client transport and typed operation wrappers |
| `remctl-read-broker-launchagent.plist` | Persistent per-user background service |

## Sealed Runtime

The installer selects a protected Python interpreter that is root-owned,
regular, non-symlinked, executable, and safe under isolated startup
(`-I -S`). Other-writable ancestors are rejected. Group-writable ancestors
are rejected when the installing user belongs to that group.

The host runtime is staged, hashed, signed, verified, and then atomically
published. Its manifest identifies every executable and Python source file
used by the privileged process. The broker revalidates the manifest and file
hashes before dispatching every request.

The default app signature is ad hoc. Because its CDHash changes on rebuild,
FDA must be removed and re-added after an upgrade. Setting
`REMCTL_CODESIGN_IDENTITY` to a Developer ID identity provides a stable code
identity across upgrades.

## Protocol and Operations

The protocol is versioned and default-deny. Each operation has an explicit
request and response schema with limits for identifiers, strings, arrays,
pagination, polling, and response size. Unknown fields and unknown operations
are rejected.

Hosted operations cover normal reads and mutation verification, including:

- lists, reminders, today, flagged, upcoming, overdue, search, stats, export
- tags, sections, templates, smart lists, groups, and group membership
- grocery categories, ordering, sharees, subtasks, alarms, and early reminders
- reminder detail, attachment metadata, preflight resolution, polling, and
  mutation readback

No hosted operation mutates Reminders data.

## Routing and Degradation

Read routing is selected with `--read-route auto|direct|host` or
`REMCTL_READ_ROUTE`.

- `auto` prefers direct access when the caller can read the store, otherwise
  it uses a healthy Capability Host.
- `direct` requires caller-side store access.
- `host` requires the Capability Host.

`REMCTL_CAPABILITY_HOST_DISABLED=1` is the emergency killswitch.
`--via-eventkit` remains an explicit, reduced-fidelity fallback.

Transport health and store readiness are separate. A reachable, correctly
identified host is not considered usable until its store/schema probe passes.
Failures are surfaced rather than converted into success-shaped empty data.

## Installation and Rollback

`install.sh` performs a transactional host installation:

1. Validate the protected Python interpreter and source inputs.
2. Build the app and sealed runtime in staging.
3. Generate the runtime manifest.
4. Compile, sign, and verify the app bundle.
5. Publish the app and LaunchAgent.
6. Start the service and verify its identity.
7. Restore the previous installation if publication or startup fails.

`uninstall.sh` removes only the exact managed app, LaunchAgent, socket, and
runtime artifacts. The user removes the app's FDA entry interactively in
System Settings.

## Acceptance Gates

The design is complete only when:

- direct reads remain blocked for an unprivileged caller
- hosted reads retain full database fidelity
- all mutations remain caller-side and unprivileged
- protocol, socket, runtime, and installer negative tests pass
- direct and hosted read results match against a real SQLite fixture
- installer failure injection proves rollback at every publication stage
- `doctor` reports transport readiness, store readiness, and effective route
  independently

Inline image rendering and some visual badges may be reduced under hosted
routing because file rendering remains caller-side. Attachment metadata stays
available in structured output.
