#!/bin/bash
# RemCTL Installer
# Installs remctl, remctl-bridge, remctl-private, remctl-permissions, shared runtime helpers,
# and the RemCTL Capability Host app bundle + LaunchAgent.

set -euo pipefail

BOOTSTRAP=0
RUN_DOCTOR=0
COMPLETION_SHELL="auto"
INSTALL_HOST=1   # Install Capability Host if swiftc is available (--no-host to skip)

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --bootstrap                 Install completions and create config for first-run onboarding
  --doctor                    Run `remctl doctor` after installation
  --shell-completions SHELL   Install completions for auto, zsh, bash, fish, or none (default: auto)
  --host                      Build and install the Capability Host (default if swiftc is available)
  --no-host                   Skip Capability Host installation
  -h, --help                  Show this help text

Notes:
  The installer copies binaries into ~/bin by default and creates rctl and reminders aliases.
  Use PREFIX="$HOME/.local" if you want ~/.local/bin instead.
  remctl-private is optional and only used by explicit --private writes.
  The Capability Host app is installed to ~/Applications/ with a LaunchAgent.
  After install, run `remctl permissions full-disk-access` to grant FDA to the Capability Host.
  Run `remctl doctor` after permissions, or pass --doctor when upgrading an already-authorized install.

Capability Host environment overrides (for testing/CI):
  REMCTL_PROTECTED_PYTHON   Override the protected Python path (must still pass safety checks)
  REMCTL_HOST_APP_DIR       Override ~/Applications install directory
  REMCTL_CODESIGN_IDENTITY  Code signing identity (default: ad-hoc "-")
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bootstrap)
            BOOTSTRAP=1
            shift
            ;;
        --doctor)
            RUN_DOCTOR=1
            shift
            ;;
        --shell-completions)
            [[ $# -ge 2 ]] || { echo "Missing value for --shell-completions" >&2; exit 1; }
            COMPLETION_SHELL="$2"
            shift 2
            ;;
        --host)
            INSTALL_HOST=1
            shift
            ;;
        --no-host)
            INSTALL_HOST=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(sed -n 's/^VERSION = "\([^"]*\)"/\1/p' "$SCRIPT_DIR/remctl" | head -n 1)"
if [[ -z "$VERSION" ]]; then
    echo "Could not determine RemCTL version from $SCRIPT_DIR/remctl" >&2
    exit 1
fi
PREFIX="${PREFIX:-$HOME}"
BIN_DIR="${REMCTL_BIN_DIR:-$PREFIX/bin}"
CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_DIR="${REMCTL_CONFIG_DIR:-$CONFIG_BASE/remctl}"

# Colors
RED='\033[38;2;224;47;55m'
GREEN='\033[38;2;97;187;70m'
YELLOW='\033[38;2;253;181;21m'
BLUE='\033[38;2;0;157;220m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

PATH_NEEDS_UPDATE=0

echo ""
echo -e "${GREEN}██████${YELLOW}██████${ORANGE:-\033[38;2;245;132;31m}█████${RED}██████${RESET}"
echo -e "${BOLD}RemCTL Installer${RESET}"
echo ""

# Ensure ~/bin exists
mkdir -p "$BIN_DIR"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR" 2>/dev/null || true

# 1. Install main CLI
echo -e "${BLUE}→${RESET} Installing remctl..."
cp "$SCRIPT_DIR/remctl" "$BIN_DIR/remctl"
chmod +x "$BIN_DIR/remctl"
echo -e "  ${GREEN}✓${RESET} remctl → $BIN_DIR/remctl"

echo -e "${BLUE}→${RESET} Installing aliases..."
ln -sf "remctl" "$BIN_DIR/rctl"
ln -sf "remctl" "$BIN_DIR/reminders"
echo -e "  ${GREEN}✓${RESET} rctl → $BIN_DIR/rctl"
echo -e "  ${GREEN}✓${RESET} reminders → $BIN_DIR/reminders"

echo -e "${BLUE}→${RESET} Installing shared runtime helpers..."
cp "$SCRIPT_DIR/remctl_runtime.py" "$BIN_DIR/remctl_runtime.py"
chmod 644 "$BIN_DIR/remctl_runtime.py"
echo -e "  ${GREEN}✓${RESET} remctl_runtime.py → $BIN_DIR/remctl_runtime.py"

cp "$SCRIPT_DIR/remctl_host.py" "$BIN_DIR/remctl_host.py"
chmod 644 "$BIN_DIR/remctl_host.py"
echo -e "  ${GREEN}✓${RESET} remctl_host.py → $BIN_DIR/remctl_host.py"

cp "$SCRIPT_DIR/remctl_host_protocol.py" "$BIN_DIR/remctl_host_protocol.py"
chmod 644 "$BIN_DIR/remctl_host_protocol.py"
echo -e "  ${GREEN}✓${RESET} remctl_host_protocol.py → $BIN_DIR/remctl_host_protocol.py"

cp "$SCRIPT_DIR/remctl_images.py" "$BIN_DIR/remctl_images.py"
chmod 644 "$BIN_DIR/remctl_images.py"
echo -e "  ${GREEN}✓${RESET} remctl_images.py → $BIN_DIR/remctl_images.py"

echo -e "${BLUE}→${RESET} Installing shared serialization helpers..."
cp "$SCRIPT_DIR/remctl_serialization.py" "$BIN_DIR/remctl_serialization.py"
chmod 644 "$BIN_DIR/remctl_serialization.py"
echo -e "  ${GREEN}✓${RESET} remctl_serialization.py → $BIN_DIR/remctl_serialization.py"

echo -e "${BLUE}→${RESET} Installing smart list helpers..."
cp "$SCRIPT_DIR/remctl_smart_lists.py" "$BIN_DIR/remctl_smart_lists.py"
chmod 644 "$BIN_DIR/remctl_smart_lists.py"
echo -e "  ${GREEN}✓${RESET} remctl_smart_lists.py → $BIN_DIR/remctl_smart_lists.py"

echo -e "${BLUE}→${RESET} Installing shell completion sources..."
mkdir -p "$BIN_DIR/completions"
for name in remctl rctl reminders; do
    completion_tmp="$BIN_DIR/completions/_${name}.tmp"
    "$BIN_DIR/$name" completion zsh > "$completion_tmp"
    mv "$completion_tmp" "$BIN_DIR/completions/_${name}"
    chmod 644 "$BIN_DIR/completions/_${name}"
    echo -e "  ${GREEN}✓${RESET} _${name} → $BIN_DIR/completions/_${name}"
done

# 2. Compile and install helpers
COMPILE_LOG="$(mktemp -t remctl-compile)"
trap 'rm -f "$COMPILE_LOG"' EXIT
if command -v swiftc &>/dev/null; then
    echo -e "${BLUE}→${RESET} Compiling remctl-bridge (Swift/EventKit)..."
    if swiftc -O \
        -framework EventKit \
        -framework Foundation \
        -o "$BIN_DIR/remctl-bridge" \
        "$SCRIPT_DIR/remctl-bridge.swift" 2>"$COMPILE_LOG"; then
        chmod +x "$BIN_DIR/remctl-bridge"
        echo -e "  ${GREEN}✓${RESET} remctl-bridge → $BIN_DIR/remctl-bridge"
    else
        echo -e "  ${RED}✗${RESET} remctl-bridge failed to compile"
        sed 's/^/    /' "$COMPILE_LOG" >&2
        exit 1
    fi

    echo -e "${BLUE}→${RESET} Compiling remctl-permissions (Swift/AppKit)..."
    if swiftc -O \
        -framework AppKit \
        -framework Foundation \
        -o "$BIN_DIR/remctl-permissions" \
        "$SCRIPT_DIR/remctl-permissions.swift" 2>"$COMPILE_LOG"; then
        chmod +x "$BIN_DIR/remctl-permissions"
        echo -e "  ${GREEN}✓${RESET} remctl-permissions → $BIN_DIR/remctl-permissions"
        if [[ -f "$SCRIPT_DIR/assets/remctl-permissions-icon.png" ]]; then
            cp "$SCRIPT_DIR/assets/remctl-permissions-icon.png" "$BIN_DIR/remctl-permissions-icon.png"
            chmod 644 "$BIN_DIR/remctl-permissions-icon.png"
            echo -e "  ${GREEN}✓${RESET} remctl-permissions-icon.png → $BIN_DIR/remctl-permissions-icon.png"
        fi
    else
        echo -e "  ${YELLOW}⚠${RESET} remctl-permissions did not compile — guided permission UI unavailable"
        sed 's/^/    /' "$COMPILE_LOG" >&2
        echo -e "    ${DIM}remctl will still print manual Full Disk Access steps${RESET}"
    fi
else
    echo -e "  ${YELLOW}⚠${RESET} swiftc not found — install Xcode Command Line Tools"
    echo -e "    ${DIM}Run: xcode-select --install${RESET}"
    echo -e "    ${DIM}remctl will fall back to AppleScript for writes and manual Full Disk Access steps${RESET}"
fi

if command -v clang &>/dev/null; then
    echo -e "${BLUE}→${RESET} Compiling remctl-private (unsupported private ReminderKit helper)..."
    if clang -fobjc-arc -O \
        -F/System/Library/PrivateFrameworks \
        -framework Foundation \
        -framework AppKit \
        -framework ReminderKit \
        -o "$BIN_DIR/remctl-private" \
        "$SCRIPT_DIR/remctl-private.m" 2>"$COMPILE_LOG"; then
        chmod +x "$BIN_DIR/remctl-private"
        echo -e "  ${GREEN}✓${RESET} remctl-private → $BIN_DIR/remctl-private"
    else
        echo -e "  ${YELLOW}⚠${RESET} remctl-private did not compile — private metadata writes unavailable"
        sed 's/^/    /' "$COMPILE_LOG" >&2
    fi
else
    echo -e "  ${YELLOW}⚠${RESET} clang not found — private metadata writes unavailable"
fi

# ─── 3. Capability Host ───────────────────────────────────────────────────────

# Canonical paths (override via env for testing).
HOST_APP_DIR="${REMCTL_HOST_APP_DIR:-$HOME/Applications}"
HOST_APP_NAME="RemCTL Capability Host.app"
HOST_BUNDLE_ID="net.macstories.remctl.capability-host"
LAUNCHAGENT_LABEL="net.macstories.remctl.read-broker"
HOST_SUPPORT_DIR="$HOME/Library/Application Support/RemCTL"
LAUNCHAGENT_PLIST="$HOME/Library/LaunchAgents/${LAUNCHAGENT_LABEL}.plist"
CODESIGN_IDENTITY="${REMCTL_CODESIGN_IDENTITY:--}"

# Safe-path helpers shared between install and uninstall logic.
_safe_regular_nolink() {
    local p="$1"
    [[ -f "$p" && ! -L "$p" ]] || return 1
}

# Validate every directory ancestor of a Python executable up to /.
#
# Safety policy (in order of priority):
#   1. Each ancestor must be root-owned (uid 0).
#   2. Each ancestor must not be a symlink.
#   3. Each ancestor must not be other-writable (world-writable = any user can inject).
#   4. Each ancestor must not be group-writable BY THE CALLER: if the group-write bit
#      is set AND the caller is a member of that gid, the caller can stage a trojan
#      interpreter.  If the caller is NOT in that group the bit is harmless — e.g.
#      root:wheel 0775 on a standard Python.org install where the user is not wheel.
#
# The leaf executable is validated separately (must not be group- or other-writable
# by anyone, regardless of caller group membership).
_validate_python_ancestors() {
    local py="$1"
    python3 -c "
import os, stat, sys

py = sys.argv[1]
# Full set of groups the current process can use for access checks.
caller_gids = set(os.getgroups())
caller_gids.add(os.getegid())

path = os.path.dirname(os.path.abspath(py))
while True:
    try:
        lst = os.lstat(path)
    except OSError as e:
        print(f'ancestor stat failed: {path}: {e}', file=sys.stderr)
        sys.exit(1)
    # Must not be a symlink.
    if stat.S_ISLNK(lst.st_mode):
        print(f'ancestor is a symlink: {path}', file=sys.stderr)
        sys.exit(1)
    # Must be root-owned.
    if lst.st_uid != 0:
        print(f'ancestor not root-owned (uid={lst.st_uid}): {path}', file=sys.stderr)
        sys.exit(1)
    # Always reject other-writable.
    if lst.st_mode & stat.S_IWOTH:
        print(
            f'ancestor is other-writable (mode={oct(lst.st_mode)}) — any user can write: {path}',
            file=sys.stderr,
        )
        sys.exit(1)
    # Reject group-writable only when the caller is actually in that group.
    if lst.st_mode & stat.S_IWGRP:
        if lst.st_gid in caller_gids:
            print(
                f'ancestor is group-writable (gid={lst.st_gid}) '
                f'and you are a member of that group — writable by current user: {path}',
                file=sys.stderr,
            )
            sys.exit(1)
        # Caller is not in the group; group-write bit poses no threat.
    if path == '/':
        break
    path = os.path.dirname(path)
" "$py" 2>&1
}

# Validate a Python executable as a safe protected runtime host.
# Leaf: regular non-symlink, root-owned, NOT group- or other-writable by anyone.
# Ancestors: root-owned, not symlink, not other-writable, not group-writable by caller.
# Runtime: must run with -I -S and report Python 3.10+.
validate_protected_python() {
    local py="$1"
    [[ -n "$py" && "${py:0:1}" == "/" ]] || return 1
    # Leaf must be a regular non-symlink file.
    _safe_regular_nolink "$py" || { echo -e "  ${RED}✗${RESET} $py is a symlink or not a regular file" >&2; return 1; }
    [[ -x "$py" ]] || { echo -e "  ${RED}✗${RESET} $py is not executable" >&2; return 1; }
    # Leaf must be root-owned.
    local uid
    uid=$(stat -f "%u" "$py" 2>/dev/null) || return 1
    [[ "$uid" == "0" ]] || { echo -e "  ${RED}✗${RESET} $py is not root-owned (uid=$uid)" >&2; return 1; }
    # Leaf must not be group- or other-writable by anyone (stricter than ancestor policy).
    python3 -c "
import os, stat, sys
st = os.stat(sys.argv[1])
if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
    print(f'leaf is group- or other-writable (mode={oct(st.st_mode)})', file=sys.stderr)
    sys.exit(1)
" "$py" 2>/dev/null || {
        echo -e "  ${RED}✗${RESET} $py is group- or other-writable" >&2; return 1
    }
    # Validate every ancestor directory up to /.
    local ancestor_err
    ancestor_err=$(_validate_python_ancestors "$py") || {
        echo -e "  ${RED}✗${RESET} Unsafe Python path for $py: $ancestor_err" >&2; return 1
    }
    # Must run with -I -S and meet version requirement.
    local ver
    ver=$("$py" -I -S -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')" 2>/dev/null) || {
        echo -e "  ${RED}✗${RESET} $py failed to run with -I -S" >&2; return 1
    }
    python3 -c "
v = '$ver'.split('.')
assert len(v) == 2 and int(v[0]) >= 3 and int(v[1]) >= 10, 'Version too old'
" 2>/dev/null || { echo -e "  ${RED}✗${RESET} $py is Python $ver (need 3.10+)" >&2; return 1; }
}

# Find a protected Python: check REMCTL_PROTECTED_PYTHON, then well-known framework paths,
# then /usr/bin/python3. Never uses Homebrew or user-installed Python.
#
# Architecture-aware: on arm64, excludes intel64-only binaries; on any arch, verifies
# the selected interpreter reports native architecture (not Rosetta-emulated x86_64).
#
# Only considers concrete versioned executables (python3.X pattern): symlinks like
# python3 → python3.14 are skipped by find -not -type l, as are pip, pydoc, and
# other helpers that do not match the python3.[0-9]* pattern.
find_protected_python() {
    # Detect machine architecture; allow override for testing.
    local ARCH="${REMCTL_TEST_ARCH:-$(uname -m)}"
    if [[ -n "${REMCTL_PROTECTED_PYTHON:-}" ]]; then
        validate_protected_python "$REMCTL_PROTECTED_PYTHON" && echo "$REMCTL_PROTECTED_PYTHON" && return 0
        echo -e "  ${RED}✗${RESET} REMCTL_PROTECTED_PYTHON=${REMCTL_PROTECTED_PYTHON} failed safety checks" >&2
        return 1
    fi
    local candidates=()
    # Official Python.org framework installs.
    # -not -type l           : skip symlinks (python3 → python3.14, etc.)
    # -name "python3.[0-9]*" : versioned executables only; excludes pip, pydoc, etc.
    while IFS= read -r py; do
        [[ -n "$py" ]] || continue
        # Exclude intel64-only wheel installs on arm64.
        if [[ "$ARCH" == "arm64" && "$py" == *-intel64* ]]; then
            continue
        fi
        candidates+=("$py")
    done < <(find /Library/Frameworks/Python.framework/Versions \
        -maxdepth 4 \
        -not -type l \
        -name "python3.[0-9]*" \
        -perm -0100 \
        2>/dev/null | sort -rV)
    # System Python.  /usr/bin/python3 is a non-symlink stub on modern macOS but
    # too old (3.9); still validate it as the last resort if it passes.
    if [[ -x /usr/bin/python3 && ! -L /usr/bin/python3 ]]; then
        candidates+=("/usr/bin/python3")
    fi
    for py in "${candidates[@]}"; do
        validate_protected_python "$py" 2>/dev/null || continue
        # Verify native architecture; rejects Rosetta-emulated x86_64 on arm64.
        local py_arch
        py_arch=$("$py" -I -S -c "import platform; print(platform.machine())" 2>/dev/null) || continue
        if [[ "$ARCH" == "arm64" && "$py_arch" != "arm64" ]]; then
            continue
        fi
        echo "$py"
        return 0
    done
    return 1
}

# Install or upgrade the Capability Host.  On failure, restore the previous generation.
install_capability_host() {
    if ! command -v swiftc &>/dev/null; then
        echo -e "  ${YELLOW}⚠${RESET} swiftc not found — Capability Host installation skipped"
        echo -e "    ${DIM}Run: xcode-select --install, then re-run ./install.sh --host${RESET}"
        return 0
    fi

    echo -e "${BLUE}→${RESET} Finding protected Python for Capability Host..."
    local PROTECTED_PYTHON
    PROTECTED_PYTHON="$(find_protected_python)" || {
        echo -e "  ${RED}✗${RESET} No safe root-owned Python 3.10+ found."
        echo -e "    ${DIM}Install Python from python.org or set REMCTL_PROTECTED_PYTHON.${RESET}"
        echo -e "    ${DIM}Never grant FDA to Homebrew or user-installed Python.${RESET}"
        return 1
    }
    echo -e "  ${GREEN}✓${RESET} Protected Python: $PROTECTED_PYTHON"

    local FINAL_APP="${HOST_APP_DIR}/${HOST_APP_NAME}"
    local BACKUP_DIR="${HOST_SUPPORT_DIR}/.host-backup"
    local BACKUP_APP="${BACKUP_DIR}/${HOST_APP_NAME}"
    local BUILD_LOG="${HOST_SUPPORT_DIR}/build/install.log"
    mkdir -p "${HOST_SUPPORT_DIR}/build"
    mkdir -p "${HOST_APP_DIR}"
    chmod 700 "${HOST_SUPPORT_DIR}"

    # Back up any existing install so we can roll back on failure.
    local HAD_PREVIOUS=0
    local BACKUP_PLIST="${BACKUP_DIR}/${LAUNCHAGENT_LABEL}.plist.bak"
    if [[ -d "$FINAL_APP" && ! -L "$FINAL_APP" ]]; then
        HAD_PREVIOUS=1
        echo -e "${BLUE}→${RESET} Backing up existing Capability Host..."
        mkdir -p "$BACKUP_DIR"
        rm -rf "$BACKUP_APP"
        cp -a "$FINAL_APP" "$BACKUP_APP"
        # Also back up the LaunchAgent plist so rollback can restore launch state.
        if [[ -f "$LAUNCHAGENT_PLIST" && ! -L "$LAUNCHAGENT_PLIST" ]]; then
            cp "$LAUNCHAGENT_PLIST" "$BACKUP_PLIST"
        else
            rm -f "$BACKUP_PLIST"
        fi
        echo -e "  ${DIM}Backup: $BACKUP_APP${RESET}"
    fi

    # Rollback helper — called on any build failure.
    _host_rollback() {
        echo -e "  ${RED}✗${RESET} Capability Host build failed — rolling back..." >&2
        # Unload any stale LaunchAgent that may reference the broken build.
        local uid; uid=$(id -u)
        launchctl bootout "gui/$uid/$LAUNCHAGENT_LABEL" 2>/dev/null || true
        rm -rf "$FINAL_APP"
        if [[ "$HAD_PREVIOUS" -eq 1 && -d "$BACKUP_APP" ]]; then
            cp -a "$BACKUP_APP" "$FINAL_APP"
            echo -e "  ${YELLOW}⚠${RESET} Restored previous Capability Host." >&2
            # Restore the LaunchAgent plist before re-bootstrapping.
            if [[ -f "$BACKUP_PLIST" ]]; then
                cp "$BACKUP_PLIST" "$LAUNCHAGENT_PLIST"
                launchctl bootstrap "gui/$uid" "$LAUNCHAGENT_PLIST" 2>/dev/null || true
            fi
        fi
    }

    # ── Build app bundle ─────────────────────────────────────────────────────
    echo -e "${BLUE}→${RESET} Building Capability Host app bundle..."
    local MACOS_DIR="${FINAL_APP}/Contents/MacOS"
    local RESOURCES_DIR="${FINAL_APP}/Contents/Resources"
    local RUNTIME_DIR="${RESOURCES_DIR}/runtime"
    local MANIFEST_PATH="${RESOURCES_DIR}/remctl-host-manifest.json"
    local EXECUTABLE="${MACOS_DIR}/remctl-capability-host"

    rm -rf "$FINAL_APP"
    mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$RUNTIME_DIR"
    chmod 700 "$FINAL_APP" "$MACOS_DIR" "$RESOURCES_DIR" "$RUNTIME_DIR"

    # Copy Info.plist.
    cp "$SCRIPT_DIR/remctl-capability-host-Info.plist" "${FINAL_APP}/Contents/Info.plist"
    chmod 644 "${FINAL_APP}/Contents/Info.plist"
    local HOST_VERSION
    HOST_VERSION=$(/usr/libexec/PlistBuddy -c \
        "Print :CFBundleShortVersionString" "${FINAL_APP}/Contents/Info.plist" 2>>"$BUILD_LOG") || {
        echo -e "  ${RED}✗${RESET} Could not read Capability Host bundle version" >&2
        _host_rollback; return 1
    }

    # Copy sealed runtime files into the app bundle.
    echo -e "${BLUE}→${RESET} Sealing runtime files..."
    local RUNTIME_FILES=(
        remctl
        remctl_host_manifest.py
        remctl_host_operations.py
        remctl_host_protocol.py
        remctl_images.py
        remctl_read_broker.py
        remctl_runtime.py
        remctl_serialization.py
        remctl_smart_lists.py
    )
    for f in "${RUNTIME_FILES[@]}"; do
        if [[ ! -f "$SCRIPT_DIR/$f" ]]; then
            echo -e "  ${RED}✗${RESET} Missing runtime source file: $SCRIPT_DIR/$f" >&2
            _host_rollback; return 1
        fi
        cp "$SCRIPT_DIR/$f" "$RUNTIME_DIR/$f"
        chmod 444 "$RUNTIME_DIR/$f"
        echo -e "  ${GREEN}✓${RESET} sealed $f"
    done

    # Generate runtime manifest (broker entrypoint is remctl_read_broker.py in runtime/).
    echo -e "${BLUE}→${RESET} Generating runtime manifest..."
    local MANIFEST_JSON
    MANIFEST_JSON=$(python3 -W ignore \
        "$SCRIPT_DIR/scripts/build_host_runtime_manifest.py" \
        --python "$PROTECTED_PYTHON" \
        --output "$MANIFEST_PATH" \
        --root "$RUNTIME_DIR" \
        --broker-entrypoint "$RUNTIME_DIR/remctl_read_broker.py" \
        --host-version "$HOST_VERSION" \
        2>>"$BUILD_LOG") || {
        echo -e "  ${RED}✗${RESET} Runtime manifest generation failed. See $BUILD_LOG" >&2
        _host_rollback; return 1
    }
    local MANIFEST_DIGEST
    MANIFEST_DIGEST=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d['manifestDigest'])" "$MANIFEST_JSON" 2>/dev/null) || {
        echo -e "  ${RED}✗${RESET} Could not extract manifest digest" >&2
        _host_rollback; return 1
    }
    chmod 444 "$MANIFEST_PATH"
    echo -e "  ${GREEN}✓${RESET} Manifest: $MANIFEST_PATH"
    echo -e "  ${DIM}Digest: $MANIFEST_DIGEST${RESET}"

    # Generate configured Swift source (substitute sentinel placeholders).
    echo -e "${BLUE}→${RESET} Compiling Capability Host (Swift)..."
    local CONFIGURED_SWIFT="${HOST_SUPPORT_DIR}/build/remctl-capability-host-configured.swift"
    # Use Python for safe substitution — no sed-based path injection.
    python3 - "$SCRIPT_DIR/remctl-capability-host.swift" "$CONFIGURED_SWIFT" \
        "$PROTECTED_PYTHON" \
        "$RUNTIME_DIR/remctl_read_broker.py" \
        "$MANIFEST_PATH" \
        "$MANIFEST_DIGEST" <<'PYEOF'
import sys, os, re

src_path, dst_path, python_exe, broker_ep, manifest_path, manifest_digest = sys.argv[1:]

with open(src_path, encoding="utf-8") as f:
    src = f.read()

def substitute(src, sentinel, value):
    # Replace the literal sentinel string (as a Swift string value).
    old = f'= "{sentinel}"'
    new = f'= "{value}"'
    if old not in src:
        print(f"ERROR: sentinel {sentinel!r} not found in Swift source", file=sys.stderr)
        sys.exit(1)
    return src.replace(old, new, 1)

# Validate: values must be safe absolute paths or a hex digest.
for label, value in [
    ("python", python_exe),
    ("broker", broker_ep),
    ("manifest", manifest_path),
]:
    if not value.startswith("/") or "\x00" in value or "\n" in value or "\r" in value:
        print(f"ERROR: unsafe {label} path: {value!r}", file=sys.stderr)
        sys.exit(1)

if not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
    print(f"ERROR: manifest digest is not 64 lowercase hex chars", file=sys.stderr)
    sys.exit(1)

src = substitute(src, "__REMCTL_CAPABILITY_HOST_PYTHON__", python_exe)
src = substitute(src, "__REMCTL_READ_BROKER_ENTRYPOINT__", broker_ep)
src = substitute(src, "__REMCTL_CAPABILITY_HOST_MANIFEST__", manifest_path)
src = substitute(src, "__REMCTL_CAPABILITY_HOST_MANIFEST_DIGEST__", manifest_digest)

os.makedirs(os.path.dirname(dst_path), exist_ok=True)
with open(dst_path, "w", encoding="utf-8") as f:
    f.write(src)
PYEOF

    if [[ $? -ne 0 ]]; then
        echo -e "  ${RED}✗${RESET} Swift source substitution failed" >&2
        _host_rollback; return 1
    fi

    # Compile the configured Swift source.
    if ! swiftc -O \
        -framework Foundation \
        -framework CryptoKit \
        -o "$EXECUTABLE" \
        "$CONFIGURED_SWIFT" \
        2>>"$BUILD_LOG"; then
        echo -e "  ${RED}✗${RESET} Capability Host Swift compilation failed. See $BUILD_LOG" >&2
        _host_rollback; return 1
    fi
    rm -f "$CONFIGURED_SWIFT"
    chmod 755 "$EXECUTABLE"
    echo -e "  ${GREEN}✓${RESET} Compiled: $EXECUTABLE"

    # Ad-hoc sign (or use REMCTL_CODESIGN_IDENTITY if set).
    echo -e "${BLUE}→${RESET} Signing Capability Host..."
    if ! codesign --sign "$CODESIGN_IDENTITY" \
        --timestamp=none \
        --force \
        "$FINAL_APP" \
        >>"$BUILD_LOG" 2>&1; then
        echo -e "  ${RED}✗${RESET} Code signing failed. See $BUILD_LOG" >&2
        _host_rollback; return 1
    fi

    # Verify codesign — hard failure for all signing identities.
    # For ad-hoc (-), --deep --strict still validates internal consistency.
    if ! codesign --verify --deep --strict "$FINAL_APP" >>"$BUILD_LOG" 2>&1; then
        echo -e "  ${RED}✗${RESET} codesign verify failed. See $BUILD_LOG" >&2
        _host_rollback; return 1
    fi

    if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
        echo -e "  ${GREEN}✓${RESET} Ad-hoc signed (identity: -)"
        echo -e ""
        echo -e "  ${YELLOW}⚠${RESET}  AD-HOC BUILD — IMPORTANT:"
        echo -e "    Each rebuild changes the CDHash. After every install or upgrade"
        echo -e "    you must remove and re-add this app in System Settings → Privacy & Security"
        echo -e "    → Full Disk Access, then re-grant access to:"
        echo -e "    ${BOLD}${FINAL_APP}${RESET}"
        echo -e "    To get a stable identity that survives upgrades, sign with a"
        echo -e "    Developer ID: REMCTL_CODESIGN_IDENTITY='Developer ID Application: …'"
        echo -e ""
    else
        echo -e "  ${GREEN}✓${RESET} Signed with Developer ID: $CODESIGN_IDENTITY"
        echo -e "  ${GREEN}✓${RESET} Stable identity — FDA grant survives upgrades"
    fi

    # Post-build health gate: run --verify mode inside the staged app.
    # This validates sealed configuration, bundle identity, and all runtime
    # manifest hashes without starting the broker or accessing Reminders.
    # A failure here prevents publication of a broken build.
    echo -e "${BLUE}→${RESET} Running Capability Host sealed verification (--verify)..."
    if ! "$EXECUTABLE" --verify >>"$BUILD_LOG" 2>&1; then
        echo -e "  ${RED}✗${RESET} Capability Host --verify failed — build integrity check not satisfied." >&2
        echo -e "    ${DIM}See $BUILD_LOG for details.${RESET}" >&2
        _host_rollback; return 1
    fi
    echo -e "  ${GREEN}✓${RESET} Sealed configuration and manifest verified"

    # ── LaunchAgent ──────────────────────────────────────────────────────────
    echo -e "${BLUE}→${RESET} Installing LaunchAgent..."
    local SOCKET_PATH="${HOME}/Library/Application Support/RemCTL/read-broker.sock"
    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$(dirname "$SOCKET_PATH")"
    chmod 700 "$(dirname "$SOCKET_PATH")"

    # Generate LaunchAgent plist with real paths substituted.
    python3 - "$SCRIPT_DIR/remctl-read-broker-launchagent.plist" \
        "$LAUNCHAGENT_PLIST" \
        "$EXECUTABLE" \
        "$SOCKET_PATH" <<'PYEOF'
import sys, plistlib

src_path, dst_path, exe, socket_path = sys.argv[1:]
with open(src_path, "rb") as f:
    plist = plistlib.load(f)

args = plist.get("ProgramArguments", [])
plist["ProgramArguments"] = [
    a.replace("__REMCTL_CAPABILITY_HOST_EXECUTABLE__", exe)
     .replace("__REMCTL_READ_BROKER_SOCKET__", socket_path)
    for a in args
]
# Verify substitution succeeded.
for a in plist["ProgramArguments"]:
    if "__REMCTL_" in a:
        print(f"ERROR: unsubstituted placeholder in ProgramArguments: {a!r}", file=sys.stderr)
        sys.exit(1)

with open(dst_path, "wb") as f:
    plistlib.dump(plist, f)
PYEOF

    if [[ $? -ne 0 ]]; then
        echo -e "  ${RED}✗${RESET} LaunchAgent plist generation failed" >&2
        _host_rollback; return 1
    fi
    chmod 644 "$LAUNCHAGENT_PLIST"
    echo -e "  ${GREEN}✓${RESET} LaunchAgent → $LAUNCHAGENT_PLIST"

    # Bootstrap or reload the LaunchAgent.
    local UID_VAL; UID_VAL=$(id -u)
    echo -e "${BLUE}→${RESET} Loading LaunchAgent (gui/$UID_VAL/$LAUNCHAGENT_LABEL)..."
    launchctl bootout "gui/$UID_VAL/$LAUNCHAGENT_LABEL" 2>/dev/null || true
    if ! launchctl bootstrap "gui/$UID_VAL" "$LAUNCHAGENT_PLIST" 2>>"$BUILD_LOG"; then
        echo -e "  ${YELLOW}⚠${RESET} launchctl bootstrap returned non-zero (may need a re-login)"
        echo -e "    ${DIM}Run: launchctl bootstrap gui/\$(id -u) $LAUNCHAGENT_PLIST${RESET}"
    else
        echo -e "  ${GREEN}✓${RESET} LaunchAgent loaded"
    fi

    # Bounded health wait: check socket up to 10 times with 1-second intervals.
    echo -e "${BLUE}→${RESET} Waiting for Capability Host socket..."
    local ATTEMPTS=0
    while [[ $ATTEMPTS -lt 10 ]]; do
        if [[ -S "$SOCKET_PATH" ]]; then
            echo -e "  ${GREEN}✓${RESET} Socket ready: $SOCKET_PATH"
            break
        fi
        sleep 1
        ATTEMPTS=$((ATTEMPTS + 1))
    done
    if [[ ! -S "$SOCKET_PATH" ]]; then
        echo -e "  ${YELLOW}⚠${RESET} Socket not yet present — host may still be starting"
        echo -e "    ${DIM}Run: remctl doctor to check host status${RESET}"
    fi

    # Clean up backup on success.
    rm -rf "$BACKUP_DIR"
    echo -e "${GREEN}✓${RESET} Capability Host installed: $FINAL_APP"
    if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
        echo -e "  ${YELLOW}Note:${RESET} Ad-hoc signed. Grant Full Disk Access to ${BOLD}${HOST_APP_NAME}${RESET} only."
        echo -e "  ${DIM}Run: remctl permissions full-disk-access${RESET}"
    fi
}

if [[ "$INSTALL_HOST" -eq 1 ]]; then
    if install_capability_host; then
        : # success path
    else
        echo -e "${YELLOW}⚠${RESET} Capability Host installation failed. Direct-mode reads continue to work."
        echo -e "${DIM}Re-run ./install.sh --host to retry.${RESET}"
    fi
fi

# 4. Check PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    PATH_NEEDS_UPDATE=1
    echo ""
    echo -e "${YELLOW}${BOLD}PATH action required${RESET}"
    echo -e "${YELLOW}RemCTL was installed to:${RESET} $BIN_DIR"
    echo -e "${YELLOW}Your current Terminal cannot find the ${BOLD}remctl${RESET}${YELLOW} command yet.${RESET}"
    echo ""
    echo -e "Add RemCTL to your zsh PATH:"
    echo -e "  ${BOLD}echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc${RESET}"
    echo ""
    echo -e "${BOLD}Then open a new Terminal window before running remctl.${RESET}"
    echo -e "${DIM}For this existing window only, you can also run: export PATH=\"$BIN_DIR:\$PATH\"${RESET}"
fi

SETUP_SHELL="$COMPLETION_SHELL"
if [[ "$SETUP_SHELL" == "none" ]]; then
    SETUP_SHELL="skip"
fi

if [[ "$SETUP_SHELL" != "skip" ]]; then
    echo -e "${BLUE}→${RESET} Running remctl setup..."
    SETUP_ARGS=("$BIN_DIR/remctl" "setup" "--shell" "$SETUP_SHELL")
    "${SETUP_ARGS[@]}"

    echo -e "${BLUE}→${RESET} Installing completions for aliases..."
    for alias_name in rctl reminders; do
        if "$BIN_DIR/$alias_name" setup --shell "$SETUP_SHELL" >/dev/null 2>&1; then
            echo -e "  ${GREEN}✓${RESET} _${alias_name} installed"
        else
            echo -e "  ${YELLOW}⚠${RESET} _${alias_name} setup skipped"
        fi
    done
fi

if [[ "$RUN_DOCTOR" -eq 1 ]]; then
    echo -e "${BLUE}→${RESET} Running remctl doctor..."
    if ! "$BIN_DIR/remctl" doctor; then
        echo -e "${YELLOW}⚠${RESET}  Doctor found setup issues. This is common before macOS permissions are granted."
        echo -e "${DIM}Run '$BIN_DIR/remctl onboard'. If Full Disk Access is missing, use '$BIN_DIR/remctl permissions full-disk-access'. Then run '$BIN_DIR/remctl doctor'.${RESET}"
        echo -e "${DIM}For agent runners, use '$BIN_DIR/remctl doctor --for-agent' in the same context that will write reminders.${RESET}"
    fi
fi

echo ""
echo -e "${GREEN}${BOLD}Done!${RESET} RemCTL v$("$BIN_DIR/remctl" --version) installed."
if [[ "$BOOTSTRAP" -eq 1 ]]; then
    echo -e "${DIM}Bootstrap is ready. Next: run 'remctl onboard', then 'remctl permissions full-disk-access', then 'remctl doctor'.${RESET}"
else
    echo -e "${DIM}Next: run 'remctl onboard' on a new Mac, then 'remctl permissions full-disk-access' for visual database-access setup.${RESET}"
fi
if [[ "$PATH_NEEDS_UPDATE" -eq 1 ]]; then
    echo -e "${YELLOW}${BOLD}Reminder:${RESET}${YELLOW} open a new Terminal window after adding $BIN_DIR to PATH, or run commands with the full path:${RESET}"
    echo -e "  ${BOLD}$BIN_DIR/remctl onboard${RESET}"
    echo -e "  Aliases also work: ${BOLD}$BIN_DIR/rctl${RESET} and ${BOLD}$BIN_DIR/reminders${RESET}"
fi
echo ""
