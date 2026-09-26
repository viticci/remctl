#!/bin/bash
# RemCTL installer. Builds and publishes one signed capability-host generation.

set -euo pipefail

BOOTSTRAP=0
RUN_DOCTOR=0
DRY_RUN=0
ADOPT_EXISTING=0
COMPLETION_SHELL="auto"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --bootstrap                 Create first-run config after installation
  --doctor                    Run `remctl doctor` after an authorized upgrade/reinstall
  --dry-run                   Build and verify without publishing or starting the service
  --adopt-existing-install    Adopt exact 1.7.1 or a reviewed prerelease-host install once
  --shell-completions SHELL   Install completions for auto, zsh, bash, fish, or none (default: auto)
  -h, --help                  Show this help text

Environment:
  PREFIX                      Install root (default: $HOME)
  REMCTL_BIN_DIR              CLI directory (default: PREFIX/bin)
  REMCTL_APP_DIR              App directory (default: PREFIX/Applications)
  REMCTL_LAUNCH_AGENT_DIR     LaunchAgent directory (default: HOME/Library/LaunchAgents)
  REMCTL_CAPABILITY_PYTHON    Protected Python 3.13+ used by the signed host
  REMCTL_CODESIGN_IDENTITY    Stable signing identity (explicit selection wins)

The installer preserves the existing rctl and reminders aliases. It builds and
strictly verifies the complete signed app before stopping the previous service.
The command-line client supports Python 3.10+; the signed host requires the
protected Python 3.13+ runtime described above.

Use --adopt-existing-install only after a normal upgrade refuses an exact 1.7.1
or reviewed prerelease-host install and you have inspected every existing RemCTL
path. Keep the same PREFIX and overrides. Never adopt unknown, modified, or
foreign files.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bootstrap) BOOTSTRAP=1; shift ;;
        --doctor) RUN_DOCTOR=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --adopt-existing-install) ADOPT_EXISTING=1; shift ;;
        --shell-completions)
            [[ $# -ge 2 ]] || { echo "Missing value for --shell-completions" >&2; exit 2; }
            COMPLETION_SHELL="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done
if [[ "$BOOTSTRAP" == "1" && "$RUN_DOCTOR" == "1" ]]; then
    echo "ERROR: --bootstrap and --doctor cannot be combined. Run './install.sh --bootstrap', then 'remctl onboard', complete any Full Disk Access step, and run 'remctl doctor'." >&2
    exit 2
fi
case "$COMPLETION_SHELL" in auto|zsh|bash|fish|none) ;; *) echo "Invalid completion shell: $COMPLETION_SHELL" >&2; exit 2 ;; esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME}"
BIN_DIR="${REMCTL_BIN_DIR:-$PREFIX/bin}"
APP_DIR="${REMCTL_APP_DIR:-$PREFIX/Applications}"
LAUNCH_AGENT_DIR="${REMCTL_LAUNCH_AGENT_DIR:-$HOME/Library/LaunchAgents}"
CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_DIR="${REMCTL_CONFIG_DIR:-$CONFIG_BASE/remctl}"
APP_NAME="RemCTL Capability Host.app"
APP_PATH="$APP_DIR/$APP_NAME"
HOST_EXECUTABLE="$APP_PATH/Contents/MacOS/RemCTL Capability Host"
AGENT_LABEL="net.macstories.remctl.capability-host"
AGENT_PATH="$LAUNCH_AGENT_DIR/$AGENT_LABEL.plist"
OLD_AGENT_PATH="$AGENT_PATH"
SOCKET_PATH="$PREFIX/Library/Application Support/RemCTL/capability-host.sock"
IDENTITY_MARKER="$BIN_DIR/.remctl-capability-host-signing-identity"
APP_MARKER="$BIN_DIR/.remctl-capability-host-app"
OWNERSHIP_MANIFEST="$BIN_DIR/.remctl-install-manifest.json"
SKIP_LAUNCHSERVICES="${REMCTL_SKIP_LAUNCHSERVICES:-0}"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

RED='\033[38;2;224;47;55m'; GREEN='\033[38;2;97;187;70m'; YELLOW='\033[38;2;253;181;21m'
BLUE='\033[38;2;0;157;220m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'
fail() { echo -e "${RED}ERROR:${RESET} $*" >&2; exit 1; }

[[ "$(uname -s 2>/dev/null || true)" == "Darwin" ]] || fail "RemCTL supports macOS only."
NATIVE_ARCH="$(uname -m 2>/dev/null || true)"
case "$NATIVE_ARCH" in arm64|x86_64) ;; *) fail "Unsupported architecture: ${NATIVE_ARCH:-unknown}" ;; esac
MACOS_TARGET="$NATIVE_ARCH-apple-macosx14.0"
for tool in swiftc clang codesign plutil security; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required. Install Xcode Command Line Tools."
done

REQUIRED_SOURCES=(
    remctl remctl_runtime.py remctl_images.py remctl_serialization.py remctl_smart_lists.py
    remctl_broker.py remctl_capability_policy.py remctl_capabilities.py remctl_mcp.py remctl_mcp_widget.html
    remctl-bridge.swift remctl-permissions.swift remctl-private.m remctl-capability-host.swift
    remctl-capability-host-Info.plist remctl-capability-host-launchagent.plist
    scripts/build_capability_archive.py
)
for source_name in "${REQUIRED_SOURCES[@]}"; do
    [[ -f "$SCRIPT_DIR/$source_name" && ! -L "$SCRIPT_DIR/$source_name" ]] || fail "Missing required source: $source_name"
done

# Test mode is isolated by both a non-home prefix and disabled LaunchServices.
CAPABILITY_SIMULATION=0
if [[ "$SKIP_LAUNCHSERVICES" == "1" && "$PREFIX" != "$HOME" ]]; then
    CAPABILITY_SIMULATION=1
    if ! /usr/bin/python3 -I -S - "$PREFIX" "$LAUNCH_AGENT_DIR" "$HOME" <<'PYTHON'
import os, pwd, sys
prefix, agent, home = [os.path.realpath(path) for path in sys.argv[1:]]
login_directory = os.path.realpath(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "Library/LaunchAgents"))
valid = prefix != home and agent != prefix and agent != login_directory and os.path.commonpath([prefix, agent]) == prefix
raise SystemExit(0 if valid else 1)
PYTHON
    then
        fail "Temp-prefix simulation requires a LaunchAgent directory under PREFIX after resolving symlinks; set REMCTL_LAUNCH_AGENT_DIR."
    fi
elif [[ "$SKIP_LAUNCHSERVICES" == "1" ]]; then
    fail "REMCTL_SKIP_LAUNCHSERVICES is allowed only with a non-home PREFIX."
fi
if [[ "${REMCTL_TEST_PUBLISH_FAIL_AT:-0}" != "0" && "$CAPABILITY_SIMULATION" != "1" ]]; then
    fail "REMCTL_TEST_PUBLISH_FAIL_AT is restricted to temp-prefix simulation."
fi
if [[ "${REMCTL_TEST_ROLLBACK_FAIL_AT:-0}" != "0" && "$CAPABILITY_SIMULATION" != "1" ]]; then
    fail "REMCTL_TEST_ROLLBACK_FAIL_AT is restricted to temp-prefix simulation."
fi
if [[ "$LAUNCH_AGENT_DIR" != "$HOME/Library/LaunchAgents" && "$CAPABILITY_SIMULATION" != "1" ]]; then
    echo "WARNING: launchd does not load $LAUNCH_AGENT_DIR at login. Use $HOME/Library/LaunchAgents for automatic startup." >&2
fi
[[ -n "$CONFIG_DIR" && "$CONFIG_DIR" == /* && "$CONFIG_DIR" != "/" && "$CONFIG_DIR" != "$HOME" && "$CONFIG_DIR" != "$CONFIG_BASE" && "$(basename "$CONFIG_DIR")" == "remctl" ]] || \
    fail "Refusing suspicious config path: $CONFIG_DIR"

CAPABILITY_PYTHON="${REMCTL_CAPABILITY_PYTHON:-}"
if [[ -z "$CAPABILITY_PYTHON" ]]; then
    for candidate in \
        /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
        /usr/local/bin/python3.13 /usr/local/bin/python3.14 \
        /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.14
    do
        if [[ -x "$candidate" ]] && "$candidate" -I -S -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,13) else 1)' >/dev/null 2>&1; then
            CAPABILITY_PYTHON="$candidate"; break
        fi
    done
fi
if [[ "$CAPABILITY_SIMULATION" == "1" && -z "$CAPABILITY_PYTHON" ]]; then CAPABILITY_PYTHON="$(command -v python3 || true)"; fi
[[ -n "$CAPABILITY_PYTHON" ]] || fail "A protected Python 3.13+ is required. Set REMCTL_CAPABILITY_PYTHON."
CAPABILITY_PYTHON="$("$CAPABILITY_PYTHON" -I -S -c 'import os,sys; print(os.path.realpath(sys.executable))' 2>/dev/null || true)"
[[ -n "$CAPABILITY_PYTHON" ]] || fail "REMCTL_CAPABILITY_PYTHON is not executable."

# Validate the interpreter before creating any destination directories.
if ! "$CAPABILITY_PYTHON" -I -S - "$CAPABILITY_PYTHON" "$CAPABILITY_SIMULATION" <<'PY'
import grp, os, pwd, shlex, stat, subprocess, sys
candidate, simulation = sys.argv[1:]
candidate = os.path.realpath(candidate)
def no_acl(path):
    try:
        result = subprocess.run(["/bin/ls", "-lde", path], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env={"LC_ALL":"C", "PATH":"/usr/bin:/bin"}, timeout=5)
    except (OSError, subprocess.SubprocessError): return False
    return result.returncode == 0 and len(result.stdout.splitlines()) == 1
def root_only_wheel():
    if 0 in {os.getgid(), os.getegid(), *os.getgroups()}: return False
    try:
        wheel, root = grp.getgrgid(0), pwd.getpwuid(0)
        if wheel.gr_name != "wheel" or root.pw_gid != 0: return False
        if any(pwd.getpwnam(name).pw_uid != 0 for name in wheel.gr_mem): return False
    except KeyError: return False
    return not any(item.pw_gid == 0 and item.pw_uid != 0 for item in pwd.getpwall())
wheel_safe = root_only_wheel()
def reject_path(path, reason, metadata=None):
    details = ""
    if metadata is not None:
        details = f" (uid={metadata.st_uid}, gid={metadata.st_gid}, mode={stat.S_IMODE(metadata.st_mode):04o})"
    print(f"Protected Python check failed: {path}{details}: {reason}.", file=sys.stderr)
    if reason == "writable by a non-root group":
        print(f"Remove group write permission: sudo chmod g-w {shlex.quote(path)}", file=sys.stderr)
        print("Other paths may need the same repair. See docs/installation.md before changing a whole framework.", file=sys.stderr)
    return False
def protected(path):
    current = os.path.realpath(path)
    while True:
        try: metadata = os.stat(current)
        except OSError as exc: return reject_path(current, str(exc))
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid != 0: return reject_path(current, "not owned by root", metadata)
        if mode & 0o002: return reject_path(current, "writable by everyone", metadata)
        if mode & 0o020 and not (metadata.st_gid == 0 and wheel_safe):
            return reject_path(current, "writable by a non-root group", metadata)
        if not no_acl(current): return reject_path(current, "has an extended ACL or its ACL could not be checked", metadata)
        parent = os.path.dirname(current)
        if parent == current: return True
        current = parent
def import_root_protected(path):
    current = os.path.realpath(path)
    while True:
        try: os.stat(current)
        except FileNotFoundError:
            parent = os.path.dirname(current)
            if parent == current: return False
            current = parent
            continue
        except OSError as exc: return reject_path(current, str(exc))
        return protected(current)
try: metadata = os.stat(candidate)
except OSError as exc:
    reject_path(candidate, str(exc))
    raise SystemExit(1)
valid = (sys.version_info >= (3,13) and os.path.isabs(candidate)
    and candidate == os.path.realpath(sys.executable) and stat.S_ISREG(metadata.st_mode)
    and os.access(candidate, os.X_OK))
if not valid:
    reject_path(candidate, "requires the canonical executable of a Python 3.13+ runtime", metadata)
if simulation != "1":
    valid = valid and protected(candidate)
    valid = valid and all(import_root_protected(entry) for entry in sys.path if entry)
raise SystemExit(0 if valid else 1)
PY
then
    fail "The capability host requires a canonical protected Python 3.13+ with root-owned, non-writable ancestry and no extended ACLs."
fi

# Read the public protocol constant as syntax, without importing or executing
# any runtime module during installation.
if ! POLICY_PROTOCOL_VERSION="$("$CAPABILITY_PYTHON" -I -S - "$SCRIPT_DIR/remctl_capability_policy.py" <<'PY'
import ast, pathlib, sys
tree = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
values = []
for node in tree.body:
    if isinstance(node, ast.Assign):
        if any(isinstance(target, ast.Name) and target.id == "PROTOCOL_VERSION" for target in node.targets):
            values.append(node.value)
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and node.target.id == "PROTOCOL_VERSION":
            values.append(node.value)
if len(values) != 1:
    raise SystemExit("expected one PROTOCOL_VERSION assignment")
try:
    value = ast.literal_eval(values[0])
except (TypeError, ValueError):
    raise SystemExit("PROTOCOL_VERSION must be a literal integer") from None
if type(value) is not int or not 1 <= value <= 2**31 - 1:
    raise SystemExit("PROTOCOL_VERSION must be a positive literal integer")
print(value)
PY
)"; then
    fail "Could not read the capability protocol version from remctl_capability_policy.py."
fi

# Select a stable identity in explicit, preserved, auto-detected order.
SIGNING_SOURCE=""; SIGNING_IDENTITY="${REMCTL_CODESIGN_IDENTITY:-}"
if [[ -n "$SIGNING_IDENTITY" ]]; then
    SIGNING_SOURCE="REMCTL_CODESIGN_IDENTITY"
elif [[ -f "$IDENTITY_MARKER" && ! -L "$IDENTITY_MARKER" ]]; then
    SIGNING_IDENTITY="$(sed -n '1p' "$IDENTITY_MARKER")"
    [[ "$(wc -l < "$IDENTITY_MARKER" | tr -d ' ')" -le 1 ]] || fail "Invalid preserved signing identity marker."
    SIGNING_SOURCE="preserved marker"
else
    SIGNING_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null | sed -n 's/^[[:space:]]*[0-9][0-9]*) \([0-9A-F][0-9A-F]*\) "Apple Development:.*$/\1/p' | head -n 1)"
    if [[ -n "$SIGNING_IDENTITY" ]]; then
        SIGNING_SOURCE="auto-detected Apple Development identity"
    elif [[ "$CAPABILITY_SIMULATION" == "1" ]]; then
        SIGNING_IDENTITY="-"; SIGNING_SOURCE="ad-hoc temp-prefix simulation"
    else
        fail "No stable Apple Development signing identity was found. Set REMCTL_CODESIGN_IDENTITY."
    fi
fi
[[ -n "$SIGNING_IDENTITY" && "$SIGNING_IDENTITY" != *$'\n'* && "$SIGNING_IDENTITY" != *$'\r'* ]] || fail "Invalid signing identity."
if [[ "$SIGNING_IDENTITY" == "-" && "$CAPABILITY_SIMULATION" != "1" ]]; then fail "Ad-hoc signing is allowed only in isolated temp-prefix simulation."; fi

# Do not take over unrelated commands. RemCTL has always used relative aliases,
# so a different file or link is a collision, not an upgrade target.
for alias_name in rctl reminders; do
    alias_path="$BIN_DIR/$alias_name"
    if [[ -e "$alias_path" || -L "$alias_path" ]]; then
        [[ -L "$alias_path" && "$(readlink "$alias_path")" == "remctl" ]] || \
            fail "Refusing to replace unrelated alias path: $alias_path"
    fi
done

echo ""; echo -e "${BOLD}RemCTL Installer${RESET}"
echo -e "${DIM}Capability Python: $CAPABILITY_PYTHON${RESET}"
echo -e "${DIM}Signing: $SIGNING_SOURCE${RESET}"; echo ""

# Stage on the same filesystems as destinations so each publish is an atomic rename.
mkdir -p "$BIN_DIR" "$APP_DIR" "$LAUNCH_AGENT_DIR"
BIN_STAGE="$(mktemp -d "$BIN_DIR/.remctl-stage.XXXXXX")"
APP_STAGE="$(mktemp -d "$APP_DIR/.remctl-app-stage.XXXXXX")"
AGENT_STAGE="$(mktemp -d "$LAUNCH_AGENT_DIR/.remctl-agent-stage.XXXXXX")"
BUILD_STAGE="$(mktemp -d "${TMPDIR:-/tmp}/remctl-build.XXXXXX")"
JOURNAL="$BUILD_STAGE/publish-journal"
TRANSACTION_ACTIVE=0; OLD_SERVICE_LOADED=0; SERVICE_QUIESCED=0; RECOVERY_FAILED=0

job_loaded() { launchctl print "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1; }
transport_available() {
    REMCTL_CAPABILITY_HOST_APP="$APP_PATH" REMCTL_CAPABILITY_HOST_SOCKET="$SOCKET_PATH" \
        "$CAPABILITY_PYTHON" -I -S -c 'import sys; sys.path.insert(0,sys.argv[1]); import remctl_broker; remctl_broker.ping(timeout=10)' "$BIN_DIR" >/dev/null 2>&1
}
wait_for_transport() {
    # Wait for the server to publish its socket before sending one bounded ping.
    # Full status includes permission probes and must not gate installation.
    for _ in {1..150}; do
        if [[ -S "$SOCKET_PATH" ]]; then
            socket_owned || return 1
            transport_available
            return $?
        fi
        sleep 0.2
    done
    return 1
}
stop_job() {
    if job_loaded; then launchctl bootout "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1 || true; fi
    for _ in {1..50}; do job_loaded || return 0; sleep 0.1; done
    return 1
}
bootstrap_job() {
    local plist="$1"
    for _ in {1..10}; do
        if job_loaded; then return 0; fi
        if launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1; then return 0; fi
        job_loaded && return 0
        sleep 0.2
    done
    return 1
}
socket_owned() {
    [[ ! -e "$SOCKET_PATH" && ! -L "$SOCKET_PATH" ]] && return 0
    "$CAPABILITY_PYTHON" -I -S - "$SOCKET_PATH" <<'PY'
import os, stat, sys
path=sys.argv[1]; parent=os.path.dirname(path)
metadata=os.lstat(path); parent_metadata=os.lstat(parent)
valid=(os.path.realpath(parent) == parent and stat.S_ISDIR(parent_metadata.st_mode)
    and parent_metadata.st_uid == os.getuid() and stat.S_IMODE(parent_metadata.st_mode) == 0o700
    and stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid()
    and stat.S_IMODE(metadata.st_mode) == 0o600)
raise SystemExit(0 if valid else 1)
PY
}
safe_remove_socket() {
    [[ ! -e "$SOCKET_PATH" && ! -L "$SOCKET_PATH" ]] && return 0
    "$CAPABILITY_PYTHON" -I -S - "$SOCKET_PATH" <<'PY'
import errno, os, socket, stat, sys
path=sys.argv[1]
parent=os.path.dirname(path)
name=os.path.basename(path)
directory=os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    parent_metadata=os.fstat(directory)
    first=os.stat(name, dir_fd=directory, follow_symlinks=False)
    valid=(
        os.path.realpath(parent) == parent
        and stat.S_ISDIR(parent_metadata.st_mode)
        and parent_metadata.st_uid == os.getuid()
        and stat.S_IMODE(parent_metadata.st_mode) == 0o700
        and stat.S_ISSOCK(first.st_mode)
        and first.st_uid == os.getuid()
        and stat.S_IMODE(first.st_mode) == 0o600
    )
    if not valid: raise SystemExit(1)
    probe=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.25)
        result=probe.connect_ex(path)
    finally: probe.close()
    if result == 0 or result not in (errno.ECONNREFUSED, errno.ENOENT): raise SystemExit(1)
    second=os.stat(name, dir_fd=directory, follow_symlinks=False)
    if (first.st_dev,first.st_ino,first.st_mode,first.st_uid) != (second.st_dev,second.st_ino,second.st_mode,second.st_uid):
        raise SystemExit(1)
    os.unlink(name, dir_fd=directory)
finally:
    os.close(directory)
PY
}
rollback_publish() {
    [[ "$TRANSACTION_ACTIVE" == "1" ]] || return 0
    if [[ ! -f "$JOURNAL" ]]; then TRANSACTION_ACTIVE=0; return 0; fi
    if ! "$CAPABILITY_PYTHON" -I -S - "$JOURNAL" "${REMCTL_TEST_ROLLBACK_FAIL_AT:-0}" "$CAPABILITY_SIMULATION" <<'PY'
import json, os, shutil, sys
path,fail_at_text,simulation=sys.argv[1:]
if fail_at_text != "0" and simulation != "1": raise SystemExit("rollback fault injection is restricted to temp-prefix simulation")
try: fail_at=int(fail_at_text)
except ValueError: raise SystemExit("invalid REMCTL_TEST_ROLLBACK_FAIL_AT")
items=[json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
errors=[]
def remove(path):
    if os.path.isdir(path) and not os.path.islink(path): shutil.rmtree(path)
    else: os.unlink(path)
for index,item in enumerate(reversed(items),1):
    source, destination, backup = item["source"], item["destination"], item["backup"]
    try:
        # A negative index counts from the oldest journal entry (-1 = the app), so a
        # test can target an entry that was actually published regardless of how
        # many pairs a generation carries.
        target = fail_at if fail_at >= 0 else len(items) + 1 + fail_at
        if fail_at and index == target: raise OSError("injected rollback failure")
        if os.path.lexists(backup):
            if os.path.lexists(destination): remove(destination)
            os.replace(backup, destination)
        elif not item["existed"] and not os.path.lexists(source) and os.path.lexists(destination):
            remove(destination)
    except OSError as exc: errors.append(f"{destination}: {exc}")
for item in items:
    destination,backup=item["destination"],item["backup"]
    if os.path.lexists(backup): errors.append(f"backup remains: {backup}")
    if item["existed"] and not os.path.lexists(destination): errors.append(f"restored destination missing: {destination}")
    if not item["existed"] and os.path.lexists(destination): errors.append(f"new destination remains: {destination}")
if errors:
    print("rollback verification failed:\n"+"\n".join(errors),file=sys.stderr)
    raise SystemExit(1)
PY
    then
        return 1
    fi
    TRANSACTION_ACTIVE=0
    return 0
}
cleanup() {
    result=$?; trap - EXIT INT TERM
    rollback_ok=1
    if [[ "$result" -ne 0 && "$TRANSACTION_ACTIVE" == "1" ]]; then
        if [[ "$CAPABILITY_SIMULATION" != "1" ]]; then
            stop_job || true
            safe_remove_socket || rollback_ok=0
            [[ -x "$LSREGISTER" ]] && "$LSREGISTER" -u "$APP_PATH" >/dev/null 2>&1 || true
        fi
        rollback_publish || rollback_ok=0
    fi
    if [[ "$result" -ne 0 && "$SERVICE_QUIESCED" == "1" && "$OLD_SERVICE_LOADED" == "1" && "$CAPABILITY_SIMULATION" != "1" ]]; then
        if [[ "$rollback_ok" == "1" && -f "$OLD_AGENT_PATH" ]]; then
            [[ -x "$LSREGISTER" && -d "$APP_PATH" ]] && "$LSREGISTER" -f "$APP_PATH" >/dev/null 2>&1 || true
            recovery_ready=0
            if job_loaded || bootstrap_job "$OLD_AGENT_PATH"; then
                if wait_for_transport; then recovery_ready=1; fi
            fi
            if [[ "$recovery_ready" != "1" ]]; then
                echo -e "${RED}RECOVERY ERROR:${RESET} The previous files were restored, but its capability-host service did not recover. Re-run the installer after checking $OLD_AGENT_PATH." >&2
                RECOVERY_FAILED=1
            fi
        else
            echo -e "${RED}RECOVERY ERROR:${RESET} Rollback was incomplete. Backups and journal were preserved; do not reinstall until they are resolved. Journal: $JOURNAL" >&2
            RECOVERY_FAILED=1
        fi
    fi
    if [[ "$rollback_ok" != "1" ]]; then
        echo -e "${RED}RECOVERY ERROR:${RESET} Rollback verification failed. Staging and journal were preserved at $BUILD_STAGE" >&2
        RECOVERY_FAILED=1
    fi
    if [[ "$RECOVERY_FAILED" != "1" ]]; then
        rm -rf -- "$BIN_STAGE" "$APP_STAGE" "$AGENT_STAGE" "$BUILD_STAGE"
    fi
    exit "$result"
}
trap cleanup EXIT INT TERM

echo -e "${BLUE}→${RESET} Compiling native helpers..."
swiftc -target "$MACOS_TARGET" -O -framework EventKit -framework Foundation -o "$BIN_STAGE/remctl-bridge" "$SCRIPT_DIR/remctl-bridge.swift"
swiftc -target "$MACOS_TARGET" -O -framework AppKit -framework Foundation -o "$BIN_STAGE/remctl-permissions" "$SCRIPT_DIR/remctl-permissions.swift"
clang -fobjc-arc -O -F/System/Library/PrivateFrameworks -framework Foundation -framework AppKit -framework ReminderKit \
    -o "$BIN_STAGE/remctl-private" "$SCRIPT_DIR/remctl-private.m"
chmod 755 "$BIN_STAGE/remctl-bridge" "$BIN_STAGE/remctl-permissions" "$BIN_STAGE/remctl-private"

echo -e "${BLUE}→${RESET} Building sealed Python archive..."
ARCHIVE="$BUILD_STAGE/remctl-capability.pyz"
ARCHIVE_MANIFEST="$BUILD_STAGE/remctl-capability-archive-manifest.json"
"$CAPABILITY_PYTHON" -I -S "$SCRIPT_DIR/scripts/build_capability_archive.py" \
    --source-root "$SCRIPT_DIR" --output "$ARCHIVE" --manifest-output "$ARCHIVE_MANIFEST"
[[ -s "$ARCHIVE" ]] || fail "Archive builder did not produce an archive."
[[ -s "$ARCHIVE_MANIFEST" ]] || fail "Archive builder did not produce its manifest."

STAGED_APP="$APP_STAGE/$APP_NAME"; STAGED_HOST="$STAGED_APP/Contents/MacOS/RemCTL Capability Host"
STAGED_RESOURCES="$STAGED_APP/Contents/Resources"
mkdir -p "$STAGED_APP/Contents/MacOS" "$STAGED_RESOURCES/CapabilityRuntime/bin"
cp "$SCRIPT_DIR/remctl-capability-host-Info.plist" "$STAGED_APP/Contents/Info.plist"
cp "$BIN_STAGE/remctl-bridge" "$STAGED_RESOURCES/CapabilityRuntime/bin/remctl-bridge"
cp "$BIN_STAGE/remctl-private" "$STAGED_RESOURCES/CapabilityRuntime/bin/remctl-private"
printf '%s\n' "$CAPABILITY_PYTHON" > "$STAGED_RESOURCES/remctl-capability-python-path"
printf '%s\n' "$SOCKET_PATH" > "$STAGED_RESOURCES/remctl-capability-host-socket-path"
printf '%s\n' "$AGENT_PATH" > "$STAGED_RESOURCES/remctl-capability-host-launch-agent-path"
chmod 644 \
    "$STAGED_RESOURCES/remctl-capability-python-path" \
    "$STAGED_RESOURCES/remctl-capability-host-socket-path" \
    "$STAGED_RESOURCES/remctl-capability-host-launch-agent-path"
"$CAPABILITY_PYTHON" -I -S - "$STAGED_RESOURCES/remctl-capability-runtime.json" "$CAPABILITY_PYTHON" "$SOCKET_PATH" "$AGENT_PATH" "$POLICY_PROTOCOL_VERSION" <<'PY'
import json, os, sys
path, python, socket, launch_agent, protocol_version = sys.argv[1:]
payload={"bundleIdentifier":"net.macstories.remctl.capability-host","protocolVersion":int(protocol_version),
    "python":python,"socket":socket,"launchAgent":launch_agent,"scope":"complete-protected-cli",
    "helpers":["remctl-bridge","remctl-private"]}
with open(path,"x",encoding="utf-8") as handle:
    json.dump(payload,handle,sort_keys=True,separators=(",",":")); handle.write("\n")
os.chmod(path,0o644)
PY

echo -e "${BLUE}→${RESET} Compiling and signing capability host..."
swiftc -target "$MACOS_TARGET" -O -framework Foundation -framework AppKit -framework CoreServices \
    -framework EventKit -framework Security -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __rctl_pyz \
    -Xlinker "$ARCHIVE" -o "$STAGED_HOST" "$SCRIPT_DIR/remctl-capability-host.swift"
chmod 755 "$STAGED_HOST"
codesign --force --deep --sign "$SIGNING_IDENTITY" "$STAGED_APP" >/dev/null
codesign --verify --deep --strict --verbose=2 "$STAGED_APP"
plutil -lint "$STAGED_APP/Contents/Info.plist" >/dev/null

# Stage the public generation.
for item in remctl remctl_runtime.py remctl_images.py remctl_serialization.py remctl_smart_lists.py \
    remctl_broker.py remctl_capability_policy.py remctl_capabilities.py remctl_mcp.py remctl_mcp_widget.html
do cp "$SCRIPT_DIR/$item" "$BIN_STAGE/$item"; done
chmod 755 "$BIN_STAGE/remctl"; chmod 644 "$BIN_STAGE"/*.py "$BIN_STAGE/remctl_mcp_widget.html"
for icon in remctl-mcp-icon.png remctl-mcp-icon-512.png; do
    if [[ -f "$SCRIPT_DIR/assets/$icon" ]]; then cp "$SCRIPT_DIR/assets/$icon" "$BIN_STAGE/$icon"; chmod 644 "$BIN_STAGE/$icon"; fi
done
if [[ -f "$SCRIPT_DIR/assets/remctl-permissions-icon.png" ]]; then
    cp "$SCRIPT_DIR/assets/remctl-permissions-icon.png" "$BIN_STAGE/remctl-permissions-icon.png"; chmod 644 "$BIN_STAGE/remctl-permissions-icon.png"
fi
printf '%s\n' "$APP_PATH" > "$BIN_STAGE/.remctl-capability-host-app"
chmod 600 "$BIN_STAGE/.remctl-capability-host-app"
if [[ "$SIGNING_IDENTITY" != "-" ]]; then
    CERTIFICATE_PREFIX="$BUILD_STAGE/remctl-signing-certificate"
    codesign -d --extract-certificates="$CERTIFICATE_PREFIX" "$STAGED_APP" >/dev/null 2>&1 || \
        fail "Could not extract the staged host signing certificate."
    [[ -f "${CERTIFICATE_PREFIX}0" && ! -L "${CERTIFICATE_PREFIX}0" ]] || \
        fail "The staged host has no extractable leaf signing certificate."
    LEAF_CERTIFICATE_SHA1="$(/usr/bin/shasum -a 1 "${CERTIFICATE_PREFIX}0" | awk '{print toupper($1)}')"
    [[ "$LEAF_CERTIFICATE_SHA1" =~ ^[0-9A-F]{40}$ ]] || fail "Could not identify the staged host signing certificate."
    echo -e "${DIM}Signing certificate SHA-1: $LEAF_CERTIFICATE_SHA1${RESET}"
    printf '%s\n' "$LEAF_CERTIFICATE_SHA1" > "$BIN_STAGE/.remctl-capability-host-signing-identity"
    chmod 600 "$BIN_STAGE/.remctl-capability-host-signing-identity"
fi
mkdir -p "$BIN_STAGE/completions"
for alias_name in remctl rctl reminders; do
    PYTHONDONTWRITEBYTECODE=1 "$CAPABILITY_PYTHON" "$BIN_STAGE/remctl" completion zsh > "$BIN_STAGE/completions/_$alias_name"; chmod 644 "$BIN_STAGE/completions/_$alias_name"
done
ln -s remctl "$BIN_STAGE/rctl"; ln -s remctl "$BIN_STAGE/reminders"

# Record exact ownership of every public artifact. A filename by itself is never
# evidence that RemCTL owns the object currently at that path.
"$CAPABILITY_PYTHON" -I -S - "$BIN_STAGE" "$BIN_STAGE/.remctl-install-manifest.json" <<'PY'
import hashlib, json, os, stat, sys
root, output = sys.argv[1:]
entries = {}
for directory, names, files in os.walk(root):
    names.sort(); files.sort()
    for name in files:
        path = os.path.join(directory, name)
        if path == output: continue
        relative = os.path.relpath(path, root)
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode):
            entries[relative] = {"type":"symlink", "target":os.readlink(path)}
        elif stat.S_ISREG(metadata.st_mode):
            with open(path,"rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            entries[relative] = {"type":"file", "sha256":digest}
        else: raise SystemExit(f"unsupported staged artifact: {relative}")
payload={"version":1,"entries":entries}
with open(output,"x",encoding="utf-8") as handle:
    json.dump(payload,handle,sort_keys=True,separators=(",",":")); handle.write("\n")
os.chmod(output,0o600)
PY

STAGED_AGENT="$AGENT_STAGE/$AGENT_LABEL.plist"
"$CAPABILITY_PYTHON" -I -S - "$SCRIPT_DIR/remctl-capability-host-launchagent.plist" "$STAGED_AGENT" "$HOST_EXECUTABLE" "$SOCKET_PATH" <<'PY'
import pathlib, sys
source,destination,host,socket=sys.argv[1:]
value=pathlib.Path(source).read_text(encoding="utf-8")
if value.count("__REMCTL_CAPABILITY_HOST__") != 1 or value.count("__REMCTL_CAPABILITY_HOST_SOCKET__") != 1:
    raise SystemExit("invalid LaunchAgent template placeholders")
pathlib.Path(destination).write_text(value.replace("__REMCTL_CAPABILITY_HOST__",host).replace("__REMCTL_CAPABILITY_HOST_SOCKET__",socket),encoding="utf-8")
PY
chmod 644 "$STAGED_AGENT"
plutil -lint "$STAGED_AGENT" >/dev/null

installed_agent_owned() {
    local path="${1:-$AGENT_PATH}"
    [[ -f "$path" && ! -L "$path" ]] || return 1
    "$CAPABILITY_PYTHON" -I -S - "$path" "$AGENT_LABEL" "$HOST_EXECUTABLE" "$SOCKET_PATH" <<'PY'
import plistlib, sys
path,label,host,socket=sys.argv[1:]
try:
    with open(path,"rb") as handle: value=plistlib.load(handle)
except (OSError, plistlib.InvalidFileException): raise SystemExit(1)
valid=(value.get("Label") == label and value.get("ProgramArguments") ==
    [host,"--run-capability-host","--socket",socket])
raise SystemExit(0 if valid else 1)
PY
}

installed_app_owned() {
    [[ -d "$APP_PATH" && ! -L "$APP_PATH" ]] || return 1
    local info="$APP_PATH/Contents/Info.plist"
    [[ -f "$info" && ! -L "$info" && -x "$HOST_EXECUTABLE" && ! -L "$HOST_EXECUTABLE" ]] || return 1
    [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$info" 2>/dev/null || true)" == "$AGENT_LABEL" ]] || return 1
    [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$info" 2>/dev/null || true)" == "RemCTL Capability Host" ]] || return 1
    /usr/bin/codesign --verify --deep --strict "$APP_PATH" >/dev/null 2>&1
}

installed_bin_owned() {
    "$CAPABILITY_PYTHON" -I -S - "$BIN_DIR" "$OWNERSHIP_MANIFEST" "$ADOPT_EXISTING" "${1:-0}" <<'PY'
import hashlib, json, os, stat, sys
root, manifest, adopt, app_contract = sys.argv[1:]
managed={
 "remctl","remctl_runtime.py","remctl_images.py","remctl_serialization.py","remctl_smart_lists.py",
 "remctl_broker.py","remctl_capability_policy.py","remctl_capabilities.py","remctl_mcp.py",
 "remctl_mcp_widget.html","remctl-mcp-icon.png","remctl-mcp-icon-512.png","remctl-bridge",
 "remctl-private","remctl-permissions","remctl-permissions-icon.png",
 ".remctl-capability-host-app",".remctl-capability-host-signing-identity",
 "completions/_remctl","completions/_rctl","completions/_reminders","rctl","reminders"}
def existing(): return {name for name in managed if os.path.lexists(os.path.join(root,name))}
def matches(path, item):
    try: metadata=os.lstat(path)
    except OSError: return False
    if item.get("type") == "symlink":
        return stat.S_ISLNK(metadata.st_mode) and os.readlink(path) == item.get("target")
    if item.get("type") != "file" or not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode): return False
    with open(path,"rb") as handle: digest=hashlib.sha256(handle.read()).hexdigest()
    return digest == item.get("sha256")
if os.path.lexists(manifest):
    try:
        metadata=os.lstat(manifest)
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600): raise ValueError
        value=json.load(open(manifest,encoding="utf-8")); entries=value["entries"]
        if value.get("version") != 1 or not isinstance(entries,dict) or set(entries) - managed: raise ValueError
        if not all(isinstance(item,dict) for item in entries.values()): raise ValueError
        current=existing(); expected=set(entries)
        problems=sorted(current ^ expected)
        problems += sorted(name for name,item in entries.items() if name in current and not matches(os.path.join(root,name),item))
        if problems:
            print("Ownership mismatch: " + ", ".join(dict.fromkeys(problems)), file=sys.stderr)
            raise SystemExit(1)
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):
        print("Ownership mismatch: .remctl-install-manifest.json", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)
present=existing()
if not present: raise SystemExit(0)
# One-time migration for the exact public files shipped by 1.7.1. Native
# helpers vary by compiler, so the exact legacy script/completion set is the
# root proof; any new-generation-only path still fails closed.
legacy={
 "remctl":"82f71a03c10922e7f68601bc6d9e8300d394495330abe2e933d137b5405090dd",
 "remctl_runtime.py":"051cdf8c72134f185f922de6489c3590d7efadfdfcc0158151c8b0508c2eee40",
 "remctl_images.py":"c25523c5e8dca106bd122103b497b653527fa6a2a8e2bc49c57a5549f5782359",
 "remctl_serialization.py":"4a23f4e8b1f5996de9a25788bce41baaf07e0efbb390a05d9ac6be04c8f6d6d9",
 "remctl_smart_lists.py":"81a500c42712e1519c5af15244ec9b55483617aef3ca6a931c800d70fc2e29ee",
 "completions/_remctl":"09539986de7736caeac55741d13281426c79639c8df02b552fc48207aa585c37",
 "completions/_rctl":"09539986de7736caeac55741d13281426c79639c8df02b552fc48207aa585c37",
 "completions/_reminders":"09539986de7736caeac55741d13281426c79639c8df02b552fc48207aa585c37"}
allowed=set(legacy)|{"remctl-bridge","remctl-private","remctl-permissions","remctl-permissions-icon.png","rctl","reminders","remctl_mcp.py","remctl_mcp_widget.html","remctl-mcp-icon.png","remctl-mcp-icon-512.png"}
valid=present <= allowed and set(legacy) <= present
for name,digest in legacy.items(): valid &= matches(os.path.join(root,name),{"type":"file","sha256":digest})
for name in ("rctl","reminders"): valid &= matches(os.path.join(root,name),{"type":"symlink","target":"remctl"})
generated=present & {"remctl-bridge","remctl-private","remctl-permissions","remctl-permissions-icon.png"}
for name in generated - {"remctl-permissions-icon.png"}:
    path=os.path.join(root,name); metadata=os.lstat(path)
    valid &= stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and os.access(path,os.X_OK)
    with open(path,"rb") as handle: valid &= handle.read(4) in (b"\xcf\xfa\xed\xfe",b"\xfe\xed\xfa\xcf")
if generated and adopt != "1": valid=False
if adopt == "1" and app_contract == "1":
    # Explicit adoption is a human-reviewed escape hatch for prerelease host
    # generations. It validates object types here; the app contract is checked
    # independently before this function is called.
    valid=all(stat.S_ISREG(os.lstat(os.path.join(root,n)).st_mode) for n in present-{"rctl","reminders"})
    valid &= all(matches(os.path.join(root,n),{"type":"symlink","target":"remctl"}) for n in present&{"rctl","reminders"})
if not valid: print("Unmanifested managed paths: " + ", ".join(sorted(present)), file=sys.stderr)
raise SystemExit(0 if valid else 1)
PY
}

APP_CONTRACT=0
if [[ -e "$AGENT_PATH" || -L "$AGENT_PATH" ]]; then
    installed_agent_owned || fail "Refusing to replace a LaunchAgent that does not match the RemCTL contract: $AGENT_PATH"
fi
if [[ -e "$APP_PATH" || -L "$APP_PATH" ]]; then
    installed_app_owned || fail "Refusing to replace an app that does not match the signed RemCTL contract: $APP_PATH"
    APP_CONTRACT=1
fi
installed_bin_owned "$APP_CONTRACT" || fail "Refusing to replace foreign, modified, or unmanifested files in $BIN_DIR. Restore the exact installed generation, move the conflicting paths, or manually review the old install and use --adopt-existing-install once."
if [[ "$CAPABILITY_SIMULATION" != "1" ]]; then
    new_signature="$(/usr/bin/codesign -d --verbose=4 -r- "$STAGED_APP" 2>&1)" || fail "Could not inspect the staged host signature."
    new_team="$(printf '%s\n' "$new_signature" | sed -n 's/^TeamIdentifier=//p')"
    new_requirement="$(printf '%s\n' "$new_signature" | sed -n 's/^designated => //p')"
    [[ -n "$new_team" && "$new_team" != "not set" && -n "$new_requirement" ]] || \
        fail "The live capability host requires a stable TeamIdentifier and designated requirement."
    if [[ -e "$APP_PATH" || -L "$APP_PATH" ]]; then
        old_signature="$(/usr/bin/codesign -d --verbose=4 -r- "$APP_PATH" 2>&1)" || fail "Could not inspect the installed host signature."
        old_team="$(printf '%s\n' "$old_signature" | sed -n 's/^TeamIdentifier=//p')"
        old_requirement="$(printf '%s\n' "$old_signature" | sed -n 's/^designated => //p')"
        [[ -n "$old_team" && "$old_team" != "not set" ]] || \
            fail "A live capability-host upgrade requires stable signed TeamIdentifiers."
        [[ "$old_team" == "$new_team" && -n "$old_requirement" && "$old_requirement" == "$new_requirement" ]] || \
            fail "The staged signature would change the capability host's TCC identity. Use the preserved signing identity."
    fi
fi

# A stale backup is unresolved recovery state. Detect every one before the old
# service is stopped, and never guess which generation should win.
assert_no_backup() {
    [[ ! -e "$1.remctl-transaction-backup" && ! -L "$1.remctl-transaction-backup" ]] || \
        fail "Stale transaction backup blocks install: $1.remctl-transaction-backup"
}
# Migrate only the old prefix-based path sealed into this signed installation.
LEGACY_AGENT_PATH="$PREFIX/Library/LaunchAgents/$AGENT_LABEL.plist"
assert_no_backup "$LEGACY_AGENT_PATH"
if [[ "$AGENT_PATH" != "$LEGACY_AGENT_PATH" && -d "$APP_PATH" &&
      "$(cat "$APP_PATH/Contents/Resources/remctl-capability-host-launch-agent-path" 2>/dev/null || true)" == "$LEGACY_AGENT_PATH" ]]; then
    installed_app_owned || fail "The legacy LaunchAgent has no valid signed host."
    [[ "$(cat "$APP_PATH/Contents/Resources/remctl-capability-host-socket-path" 2>/dev/null || true)" == "$SOCKET_PATH" ]] || fail "The legacy host socket does not match this installation."
    if [[ -e "$LEGACY_AGENT_PATH" || -L "$LEGACY_AGENT_PATH" ]]; then
        installed_agent_owned "$LEGACY_AGENT_PATH" || fail "Refusing to migrate a legacy LaunchAgent that does not match the RemCTL contract."
        [[ ! -e "$AGENT_PATH" && ! -L "$AGENT_PATH" ]] || fail "Both old and new LaunchAgent paths exist; resolve the duplicate before migrating."
        OLD_AGENT_PATH="$LEGACY_AGENT_PATH"
    fi
fi
assert_no_backup "$APP_PATH"; assert_no_backup "$AGENT_PATH"; assert_no_backup "$OLD_AGENT_PATH"
for item in remctl remctl_runtime.py remctl_images.py remctl_serialization.py remctl_smart_lists.py \
    remctl_broker.py remctl_capability_policy.py remctl_capabilities.py remctl_mcp.py remctl_mcp_widget.html \
    remctl-bridge remctl-private remctl-permissions .remctl-capability-host-app rctl reminders
do assert_no_backup "$BIN_DIR/$item"; done
for icon in remctl-mcp-icon.png remctl-mcp-icon-512.png; do if [[ -f "$BIN_STAGE/$icon" ]]; then assert_no_backup "$BIN_DIR/$icon"; fi; done
assert_no_backup "$OWNERSHIP_MANIFEST"
if [[ -f "$BIN_STAGE/remctl-permissions-icon.png" ]]; then assert_no_backup "$BIN_DIR/remctl-permissions-icon.png"; fi
if [[ -f "$BIN_STAGE/.remctl-capability-host-signing-identity" ]]; then assert_no_backup "$IDENTITY_MARKER"; fi
for alias_name in remctl rctl reminders; do assert_no_backup "$BIN_DIR/completions/_$alias_name"; done

if [[ "$DRY_RUN" == "1" ]]; then
    echo -e "${GREEN}Dry run complete.${RESET} The app built and passed strict signature verification."
    exit 0
fi

if [[ "$CAPABILITY_SIMULATION" != "1" ]]; then
    echo -e "${BLUE}→${RESET} Quiescing previous capability host..."
    if job_loaded; then
        installed_agent_owned "$OLD_AGENT_PATH" || fail "The loaded $AGENT_LABEL job has no exact installer-owned plist; refusing to stop it."
        OLD_SERVICE_LOADED=1
    fi
    socket_owned || fail "A capability-host socket exists but is not an exact current-user 0600 socket in its canonical 0700 directory; refusing to stop or overwrite anything."
    SERVICE_QUIESCED=1
    stop_job || fail "The previous capability host did not stop; nothing was published."
    safe_remove_socket || fail "Refusing to remove a foreign or unsafe capability-host socket."
fi

echo -e "${BLUE}→${RESET} Publishing one transactional generation..."
PAIRS="$BUILD_STAGE/publish-pairs"; : > "$PAIRS"
add_pair() { printf '%s\0%s\0' "$1" "$2" >> "$PAIRS"; }
add_pair "$STAGED_APP" "$APP_PATH"; add_pair "$STAGED_AGENT" "$AGENT_PATH"
if [[ "$OLD_AGENT_PATH" != "$AGENT_PATH" ]]; then add_pair "" "$OLD_AGENT_PATH"; fi
for item in remctl remctl_runtime.py remctl_images.py remctl_serialization.py remctl_smart_lists.py \
    remctl_broker.py remctl_capability_policy.py remctl_capabilities.py remctl_mcp.py remctl_mcp_widget.html \
    remctl-bridge remctl-private remctl-permissions .remctl-capability-host-app rctl reminders
do add_pair "$BIN_STAGE/$item" "$BIN_DIR/$item"; done
for icon in remctl-mcp-icon.png remctl-mcp-icon-512.png; do if [[ -f "$BIN_STAGE/$icon" ]]; then add_pair "$BIN_STAGE/$icon" "$BIN_DIR/$icon"; fi; done
add_pair "$BIN_STAGE/.remctl-install-manifest.json" "$OWNERSHIP_MANIFEST"
if [[ -f "$BIN_STAGE/remctl-permissions-icon.png" ]]; then add_pair "$BIN_STAGE/remctl-permissions-icon.png" "$BIN_DIR/remctl-permissions-icon.png"; fi
if [[ -f "$BIN_STAGE/.remctl-capability-host-signing-identity" ]]; then add_pair "$BIN_STAGE/.remctl-capability-host-signing-identity" "$IDENTITY_MARKER"; fi
for alias_name in remctl rctl reminders; do add_pair "$BIN_STAGE/completions/_$alias_name" "$BIN_DIR/completions/_$alias_name"; done

TRANSACTION_ACTIVE=1
"$CAPABILITY_PYTHON" -I -S - "$PAIRS" "$JOURNAL" "${REMCTL_TEST_PUBLISH_FAIL_AT:-0}" "$CAPABILITY_SIMULATION" <<'PY'
import json, os, shutil, sys
pairs_path,journal_path,fail_at_text,simulation=sys.argv[1:]
if fail_at_text != "0" and simulation != "1": raise SystemExit("publish fault injection is restricted to temp-prefix simulation")
try: fail_at=int(fail_at_text)
except ValueError: raise SystemExit("invalid REMCTL_TEST_PUBLISH_FAIL_AT")
raw=open(pairs_path,"rb").read().split(b"\0")
if raw and raw[-1] == b"": raw.pop()
if len(raw)%2: raise SystemExit("invalid publish pair stream")
pairs=[(os.fsdecode(raw[i]),os.fsdecode(raw[i+1])) for i in range(0,len(raw),2)]
entries=[]
for source,destination in pairs:
    backup=destination+".remctl-transaction-backup"
    if os.path.lexists(backup): raise SystemExit(f"stale transaction backup blocks install: {backup}")
    os.makedirs(os.path.dirname(destination),mode=0o700,exist_ok=True)
    entries.append({"source":source,"destination":destination,"backup":backup,"existed":os.path.lexists(destination)})
with open(journal_path,"x",encoding="utf-8") as journal:
    for entry in entries:
        journal.write(json.dumps(entry,sort_keys=True)+"\n"); journal.flush(); os.fsync(journal.fileno())
for index,((source,destination),entry) in enumerate(zip(pairs,entries),1):
    if entry["existed"]: os.replace(destination,entry["backup"])
    if source: os.replace(source,destination)
    if fail_at and index == fail_at: raise OSError("injected publish failure")
PY

if [[ "$CAPABILITY_SIMULATION" != "1" ]]; then
    echo -e "${BLUE}→${RESET} Starting and verifying capability host..."
    [[ -x "$LSREGISTER" ]] || fail "LaunchServices registration tool is unavailable; restoring the previous generation."
    "$LSREGISTER" -f "$APP_PATH" >/dev/null || fail "LaunchServices could not register the capability host; restoring the previous generation."
    bootstrap_job "$AGENT_PATH" || fail "launchd did not accept the new capability host after bounded retries; restoring the previous generation."
    wait_for_transport || fail "The new capability host did not become ready; restoring the previous generation."
fi

# Commit only after transport health. Until here the EXIT trap can restore every
# file. From this point the new service is authoritative; backup cleanup is
# best-effort so a cleanup error cannot trigger a partial rollback.
TRANSACTION_ACTIVE=0
if ! "$CAPABILITY_PYTHON" -I -S - "$JOURNAL" <<'PY'
import json, os, shutil, sys
for line in open(sys.argv[1],encoding="utf-8"):
    backup=json.loads(line)["backup"]
    if os.path.isdir(backup) and not os.path.islink(backup): shutil.rmtree(backup)
    elif os.path.lexists(backup): os.unlink(backup)
PY
then
    echo -e "${YELLOW}The new generation is active, but old transaction backups could not be removed. Resolve the reported *.remctl-transaction-backup files before reinstalling.${RESET}" >&2
fi
SERVICE_QUIESCED=0

# The HTTP process imports the client modules once. Reload an already-running
# endpoint after publishing, or it will keep serving the previous runtime.
if [[ "$CAPABILITY_SIMULATION" != "1" ]]; then
    if ! "$CAPABILITY_PYTHON" -I -S - "$BIN_DIR" <<'PY'
import sys, time
sys.path.insert(0, sys.argv[1])
import remctl_mcp
before = remctl_mcp.http_agent_status()
if before["loaded"]:
    config = remctl_mcp.load_http_config()
    if config is None or not remctl_mcp.restart_http_agent():
        raise SystemExit("Could not reload the active MCP HTTP endpoint.")
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        after = remctl_mcp.http_agent_status()
        if after["running"] and after["pid"] != before["pid"] and remctl_mcp.http_health(config)["ok"]:
            print("Reloaded and verified the active MCP HTTP endpoint.")
            break
        time.sleep(0.25)
    else:
        raise SystemExit("The MCP HTTP endpoint did not become healthy after reloading.")
PY
    then
        fail "The new RemCTL generation is installed, but the MCP HTTP endpoint did not reload. Check 'remctl mcp status' before using it."
    fi
fi

if [[ "$BOOTSTRAP" == "1" || "$COMPLETION_SHELL" != "none" ]]; then
    setup_shell="$COMPLETION_SHELL"
    [[ "$setup_shell" == "none" ]] && setup_shell="skip"
    if ! PYTHONDONTWRITEBYTECODE=1 "$BIN_DIR/remctl" setup --shell "$setup_shell"; then
        if [[ "$BOOTSTRAP" == "1" ]]; then fail "RemCTL installed, but first-run bootstrap setup failed."; fi
        echo -e "${YELLOW}First-run setup was skipped.${RESET}"
    fi
fi
if [[ "$COMPLETION_SHELL" != "none" ]]; then
    PYTHONDONTWRITEBYTECODE=1 "$BIN_DIR/rctl" setup --shell "$COMPLETION_SHELL" >/dev/null 2>&1 || true
    PYTHONDONTWRITEBYTECODE=1 "$BIN_DIR/reminders" setup --shell "$COMPLETION_SHELL" >/dev/null 2>&1 || true
fi
if [[ "$RUN_DOCTOR" == "1" ]]; then
    PYTHONDONTWRITEBYTECODE=1 "$BIN_DIR/remctl" doctor || echo -e "${YELLOW}Doctor found setup issues. Resolve the reported checks, then rerun 'remctl doctor --for-agent'.${RESET}"
fi

echo ""; echo -e "${GREEN}${BOLD}Done!${RESET} RemCTL v$(PYTHONDONTWRITEBYTECODE=1 "$BIN_DIR/remctl" --version) installed."
echo -e "${DIM}Capability host: $APP_PATH${RESET}"
if [[ "$APP_CONTRACT" == "0" ]]; then
    echo -e "${DIM}First capability-host install: run '$BIN_DIR/remctl onboard'. It requests Reminders and Automation access and opens the exact-host Full Disk Access guide only if needed.${RESET}"
    echo -e "${DIM}If you change Full Disk Access, add only '$APP_PATH', restart the host with 'launchctl kickstart -k \"gui/\$(id -u)/$AGENT_LABEL\"', then run '$BIN_DIR/remctl doctor' (agents: '$BIN_DIR/remctl doctor --for-agent --json').${RESET}"
else
    echo -e "${DIM}Upgrade/reinstall complete. The signed host identity was preserved, so existing permission grants remain valid.${RESET}"
    echo -e "${DIM}Run '$BIN_DIR/remctl doctor' (agents: '$BIN_DIR/remctl doctor --for-agent --json'). Run '$BIN_DIR/remctl onboard' only if doctor reports host permission trouble.${RESET}"
fi
echo -e "${DIM}Use '$BIN_DIR/remctl permissions full-disk-access' only to reopen or repair the exact-host Full Disk Access guide.${RESET}"
echo -e "${DIM}Connect AI apps: '$BIN_DIR/remctl onboard' offers it, or run '$BIN_DIR/remctl mcp install' (Claude Code, Codex, Claude Desktop/Cowork) any time.${RESET}"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then echo -e "${YELLOW}Add $BIN_DIR to PATH, then open a new Terminal window.${RESET}"; fi
