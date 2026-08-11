#!/bin/bash
# Remove only the optional RemCTL Agent Helper files.

set -euo pipefail

DRY_RUN=0
PREFIX="${PREFIX:-$HOME}"
BIN_DIR="${REMCTL_BIN_DIR:-$PREFIX/bin}"
APP_PARENT="${REMCTL_AGENT_APP_DIR:-$HOME/Applications}"
APP_PATH="$APP_PARENT/RemCTL Agent Helper.app"
CLIENT_PATH="$BIN_DIR/remctl-agent"

usage() {
    cat <<'EOF'
Usage: scripts/uninstall-agent-helper.sh [--dry-run]

Removes only the optional RemCTL Agent Helper app and remctl-agent client.
It does not remove RemCTL itself or revoke macOS privacy permissions.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[[ "$(basename "$APP_PATH")" == "RemCTL Agent Helper.app" ]] || {
    echo "Refusing unexpected app path: $APP_PATH" >&2
    exit 1
}
[[ "$(basename "$CLIENT_PATH")" == "remctl-agent" ]] || {
    echo "Refusing unexpected client path: $CLIENT_PATH" >&2
    exit 1
}

if [[ "$DRY_RUN" -eq 1 ]]; then
    [[ -e "$APP_PATH" ]] && echo "would remove $APP_PATH"
    [[ -e "$CLIENT_PATH" ]] && echo "would remove $CLIENT_PATH"
    exit 0
fi

if [[ -e "$APP_PATH" ]]; then
    osascript -e 'on run argv' \
        -e 'tell application (item 1 of argv) to quit' \
        -e 'end run' \
        -- "$APP_PATH" >/dev/null 2>&1 || true
    rm -rf -- "$APP_PATH"
    echo "removed $APP_PATH"
fi
if [[ -e "$CLIENT_PATH" ]]; then
    rm -f -- "$CLIENT_PATH"
    echo "removed $CLIENT_PATH"
fi
echo "Privacy grants were not changed; revoke them manually in System Settings if desired."
