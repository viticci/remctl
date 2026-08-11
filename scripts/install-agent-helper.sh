#!/bin/bash
# Build and install the optional, narrowly permissioned RemCTL Agent Helper.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PREFIX="${PREFIX:-$HOME}"
BIN_DIR="${REMCTL_BIN_DIR:-$PREFIX/bin}"
APP_PARENT="${REMCTL_AGENT_APP_DIR:-$HOME/Applications}"
APP_PATH="$APP_PARENT/RemCTL Agent Helper.app"
SIGN_IDENTITY="${REMCTL_CODESIGN_IDENTITY:--}"
SWIFT_TARGET="${REMCTL_SWIFT_TARGET:-$(uname -m)-apple-macos14.0}"

usage() {
    cat <<'EOF'
Usage: scripts/install-agent-helper.sh

Builds and installs:
  ~/Applications/RemCTL Agent Helper.app
  ~/bin/remctl-agent

Environment overrides:
  PREFIX                       Prefix containing bin/remctl (default: $HOME)
  REMCTL_BIN_DIR               Exact directory containing installed RemCTL files
  REMCTL_AGENT_APP_DIR         Parent directory for the helper app (default: ~/Applications)
  REMCTL_CODESIGN_IDENTITY     codesign identity (default: ad-hoc signing)
  REMCTL_SWIFT_TARGET          Swift target triple (default: current architecture, macOS 14)

Run ./install.sh first. The helper is optional and intended for agent hosts that
should not receive Full Disk Access themselves.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
elif [[ $# -ne 0 ]]; then
    echo "Unknown argument: $1" >&2
    usage >&2
    exit 2
fi

for command in swiftc codesign ditto plutil; do
    command -v "$command" >/dev/null || {
        echo "Required command is missing: $command" >&2
        exit 1
    }
done

REQUIRED_RUNTIME_FILES=(
    remctl
    remctl_runtime.py
    remctl_serialization.py
    remctl_smart_lists.py
    remctl_images.py
    remctl-bridge
)
OPTIONAL_RUNTIME_FILES=(
    remctl-private
    remctl-permissions
)
for file in "${REQUIRED_RUNTIME_FILES[@]}"; do
    [[ -e "$BIN_DIR/$file" ]] || {
        echo "Installed RemCTL runtime is missing: $BIN_DIR/$file" >&2
        echo "Run ./install.sh before installing the Agent Helper." >&2
        exit 1
    }
done

BUILD_DIR="$(mktemp -d -t remctl-agent-helper)"
cleanup() {
    rm -rf -- "$BUILD_DIR"
}
trap cleanup EXIT

BUILD_APP="$BUILD_DIR/RemCTL Agent Helper.app"
mkdir -p "$BUILD_APP/Contents/MacOS" "$BUILD_APP/Contents/Resources/Runtime"
mkdir -p "$BUILD_DIR/module-cache"
cp "$REPO_DIR/agent-helper/Info.plist" "$BUILD_APP/Contents/Info.plist"
cp "$REPO_DIR/agent-helper/RemCTLAgentHelper.sdef" "$BUILD_APP/Contents/Resources/RemCTLAgentHelper.sdef"
REMCTL_VERSION="$("$BIN_DIR/remctl" --version)"
plutil -replace CFBundleShortVersionString -string "$REMCTL_VERSION" "$BUILD_APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$REMCTL_VERSION" "$BUILD_APP/Contents/Info.plist"

swiftc -module-cache-path "$BUILD_DIR/module-cache" -target "$SWIFT_TARGET" -O \
    -framework AppKit -framework Foundation \
    -o "$BUILD_APP/Contents/MacOS/RemCTLAgentHelper" \
    "$REPO_DIR/agent-helper/RemCTLAgentHelper.swift"
swiftc -module-cache-path "$BUILD_DIR/module-cache" -target "$SWIFT_TARGET" -O \
    -framework Foundation \
    -o "$BUILD_DIR/remctl-agent" \
    "$REPO_DIR/agent-helper/remctl-agent.swift"

for file in "${REQUIRED_RUNTIME_FILES[@]}"; do
    cp "$BIN_DIR/$file" "$BUILD_APP/Contents/Resources/Runtime/$file"
done
for file in "${OPTIONAL_RUNTIME_FILES[@]}"; do
    if [[ -e "$BIN_DIR/$file" ]]; then
        cp "$BIN_DIR/$file" "$BUILD_APP/Contents/Resources/Runtime/$file"
    else
        echo "Warning: optional runtime is missing: $BIN_DIR/$file" >&2
    fi
done
chmod +x \
    "$BUILD_APP/Contents/MacOS/RemCTLAgentHelper" \
    "$BUILD_APP/Contents/Resources/Runtime/remctl" \
    "$BUILD_APP/Contents/Resources/Runtime/remctl-bridge"
for file in remctl-private remctl-permissions; do
    if [[ -e "$BUILD_APP/Contents/Resources/Runtime/$file" ]]; then
        chmod +x "$BUILD_APP/Contents/Resources/Runtime/$file"
    fi
done

codesign --force --deep --options runtime --timestamp=none \
    --sign "$SIGN_IDENTITY" "$BUILD_APP"
codesign --force --options runtime --timestamp=none \
    --identifier com.viticci.remctl.agent-client \
    --sign "$SIGN_IDENTITY" "$BUILD_DIR/remctl-agent"
codesign --verify --deep --strict "$BUILD_APP"
codesign --verify --strict "$BUILD_DIR/remctl-agent"

mkdir -p "$APP_PARENT" "$BIN_DIR"
if [[ -e "$APP_PATH" ]]; then
    [[ "$(basename "$APP_PATH")" == "RemCTL Agent Helper.app" ]] || {
        echo "Refusing to replace unexpected app path: $APP_PATH" >&2
        exit 1
    }
    rm -rf -- "$APP_PATH"
fi
ditto "$BUILD_APP" "$APP_PATH"
install -m 755 "$BUILD_DIR/remctl-agent" "$BIN_DIR/remctl-agent"

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [[ -x "$LSREGISTER" ]]; then
    "$LSREGISTER" -f "$APP_PATH" >/dev/null 2>&1 || true
fi

echo "Installed: $APP_PATH"
echo "Installed: $BIN_DIR/remctl-agent"
echo ""
echo "Next steps:"
echo "  1. Run: $BIN_DIR/remctl-agent onboard"
echo "  2. Allow the current host to automate RemCTL Agent Helper when macOS asks."
echo "  3. Add only RemCTL Agent Helper.app to Full Disk Access."
echo "  4. Allow Reminders access for RemCTL Agent Helper when macOS asks."
echo "  5. Relaunch the helper, then run: $BIN_DIR/remctl-agent doctor --json"
