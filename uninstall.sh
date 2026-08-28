#!/bin/bash
# RemCTL Uninstaller
# Removes only files created by install.sh plus the optional config directory.

set -euo pipefail

DRY_RUN=0
KEEP_CONFIG=0

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [options]

Options:
  --dry-run       Print what would be removed without deleting anything
  --keep-config   Keep the RemCTL config directory
  -h, --help      Show this help text

Notes:
  With REMCTL_BIN_DIR set, only that directory is checked.
  With PREFIX set, only PREFIX/bin is checked.
  Otherwise both ~/bin and ~/.local/bin are checked.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --keep-config)
            KEEP_CONFIG=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

RED='\033[38;2;224;47;55m'
GREEN='\033[38;2;97;187;70m'
YELLOW='\033[38;2;253;181;21m'
BLUE='\033[38;2;0;157;220m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

FILES=(
    remctl
    remctl_runtime.py
    remctl_host.py
    remctl_host_protocol.py
    remctl_serialization.py
    remctl_smart_lists.py
    remctl_images.py
    remctl-bridge
    remctl-private
    remctl-permissions
    remctl-permissions-icon.png
    completions/_remctl
    completions/_rctl
    completions/_reminders
)

if [[ -n "${REMCTL_BIN_DIR:-}" ]]; then
    BIN_DIRS=("$REMCTL_BIN_DIR")
elif [[ -n "${PREFIX:-}" ]]; then
    BIN_DIRS=("$PREFIX/bin")
else
    BIN_DIRS=("$HOME/bin" "$HOME/.local/bin")
fi

CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_DIR="${REMCTL_CONFIG_DIR:-$CONFIG_BASE/remctl}"
REMOVED=0

echo ""
echo -e "${BOLD}RemCTL Uninstaller${RESET}"
if [[ "$DRY_RUN" -eq 1 ]]; then
    echo -e "${YELLOW}(dry run; nothing will be deleted)${RESET}"
fi
echo ""

remove_file() {
    local path="$1"
    if [[ -e "$path" || -L "$path" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo -e "  ${YELLOW}would remove${RESET} $path"
        else
            rm -f -- "$path"
            echo -e "  ${GREEN}removed${RESET} $path"
        fi
        REMOVED=$((REMOVED + 1))
    fi
}

remove_empty_dir() {
    local path="$1"
    if [[ -d "$path" ]]; then
        if [[ -z "$(find "$path" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
            if [[ "$DRY_RUN" -eq 1 ]]; then
                echo -e "  ${YELLOW}would remove empty dir${RESET} $path"
            else
                rmdir -- "$path"
                echo -e "  ${GREEN}removed empty dir${RESET} $path"
            fi
            REMOVED=$((REMOVED + 1))
        else
            echo -e "  ${DIM}left non-empty dir${RESET} $path"
        fi
    fi
}

safe_config_dir() {
    local path="$1"
    [[ -n "$path" ]] || return 1
    [[ "$path" != "/" ]] || return 1
    [[ "$path" != "$HOME" ]] || return 1
    [[ "$path" != "$CONFIG_BASE" ]] || return 1
    [[ "$(basename "$path")" == "remctl" ]] || return 1
}

ALIASES=(
    rctl
    reminders
)

remove_alias_symlink() {
    local bin_dir="$1"
    local alias_name="$2"
    local alias_path="$bin_dir/$alias_name"
    if [[ -L "$alias_path" ]]; then
        local target
        target="$(readlink "$alias_path")" || return 0
        # Only remove if it's a relative or absolute symlink to remctl in the same directory
        if [[ "$target" == "remctl" || "$(basename "$target")" == "remctl" ]]; then
            if [[ "$DRY_RUN" -eq 1 ]]; then
                echo -e "  ${YELLOW}would remove alias${RESET} $alias_path"
            else
                rm -f -- "$alias_path"
                echo -e "  ${GREEN}removed alias${RESET} $alias_path"
            fi
            REMOVED=$((REMOVED + 1))
        else
            echo -e "  ${DIM}left unrelated symlink${RESET} $alias_path"
        fi
    fi
}

for bin_dir in "${BIN_DIRS[@]}"; do
    if [[ ! -d "$bin_dir" ]]; then
        echo -e "${DIM}Skipping missing $bin_dir${RESET}"
        continue
    fi
    echo -e "${BLUE}->${RESET} Checking $bin_dir"
    for file in "${FILES[@]}"; do
        remove_file "$bin_dir/$file"
    done
    for alias_name in "${ALIASES[@]}"; do
        remove_alias_symlink "$bin_dir" "$alias_name"
    done
    remove_empty_dir "$bin_dir/completions"
done

if [[ "$KEEP_CONFIG" -eq 1 ]]; then
    echo -e "${DIM}Keeping config directory: $CONFIG_DIR${RESET}"
elif [[ -d "$CONFIG_DIR" ]]; then
    if ! safe_config_dir "$CONFIG_DIR"; then
        echo -e "${RED}Refusing to remove suspicious config path:${RESET} $CONFIG_DIR" >&2
        exit 1
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo -e "${YELLOW}would remove config dir${RESET} $CONFIG_DIR"
    else
        rm -rf -- "$CONFIG_DIR"
        echo -e "${GREEN}removed config dir${RESET} $CONFIG_DIR"
    fi
    REMOVED=$((REMOVED + 1))
fi

# ─── Capability Host ──────────────────────────────────────────────────────────

LAUNCHAGENT_LABEL="net.macstories.remctl.read-broker"
LAUNCHAGENT_PLIST="${HOME}/Library/LaunchAgents/${LAUNCHAGENT_LABEL}.plist"
HOST_APP_DIR="${REMCTL_HOST_APP_DIR:-$HOME/Applications}"
HOST_APP="${HOST_APP_DIR}/RemCTL Capability Host.app"
HOST_SUPPORT_DIR="${HOME}/Library/Application Support/RemCTL"
HOST_SOCKET="${HOST_SUPPORT_DIR}/read-broker.sock"
HOST_HEALTH_FILE="${HOST_SUPPORT_DIR}/host-health.json"
HOST_MANIFEST_FILE="${HOST_SUPPORT_DIR}/host-manifest.json"

_safe_host_app() {
    local p="$1"
    # Refuse to remove if: symlink, not a directory, name doesn't match, or not owned by user.
    [[ ! -L "$p" ]] || return 1
    [[ -d "$p" ]] || return 1
    [[ "$(basename "$p")" == "RemCTL Capability Host.app" ]] || return 1
    local owner; owner=$(stat -f "%u" "$p" 2>/dev/null) || return 1
    [[ "$owner" == "$(id -u)" ]] || return 1
}

echo -e "${BLUE}->${RESET} Checking Capability Host"

# Unload the LaunchAgent first.
if launchctl list "$LAUNCHAGENT_LABEL" &>/dev/null; then
    LA_UID=$(id -u)
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo -e "  ${YELLOW}would unload${RESET} gui/$LA_UID/$LAUNCHAGENT_LABEL"
    else
        launchctl bootout "gui/$LA_UID/$LAUNCHAGENT_LABEL" 2>/dev/null || true
        echo -e "  ${GREEN}unloaded${RESET} $LAUNCHAGENT_LABEL"
    fi
    REMOVED=$((REMOVED + 1))
fi

remove_file "$LAUNCHAGENT_PLIST"

# Remove app bundle (after ownership/type safety check).
if [[ -e "$HOST_APP" || -L "$HOST_APP" ]]; then
    if _safe_host_app "$HOST_APP"; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo -e "  ${YELLOW}would remove app${RESET} $HOST_APP"
        else
            rm -rf -- "$HOST_APP"
            echo -e "  ${GREEN}removed app${RESET} $HOST_APP"
        fi
        REMOVED=$((REMOVED + 1))
    else
        echo -e "  ${RED}refusing to remove suspicious path${RESET} $HOST_APP" >&2
    fi
fi

# Remove the socket (if it's a socket file, not a symlink, owned by user).
if [[ -e "$HOST_SOCKET" ]]; then
    SOCK_OWNER=$(stat -f "%u" "$HOST_SOCKET" 2>/dev/null) || SOCK_OWNER=""
    if [[ ! -L "$HOST_SOCKET" && -S "$HOST_SOCKET" && "$SOCK_OWNER" == "$(id -u)" ]]; then
        remove_file "$HOST_SOCKET"
    else
        echo -e "  ${DIM}left non-socket or foreign file${RESET} $HOST_SOCKET"
    fi
fi

# Remove host health and manifest state files (safe plain files).
for state_file in "$HOST_HEALTH_FILE" "$HOST_MANIFEST_FILE"; do
    if [[ -f "$state_file" && ! -L "$state_file" ]]; then
        remove_file "$state_file"
    fi
done

# Remove the support directory only if it is now empty.
remove_empty_dir "${HOST_SUPPORT_DIR}/build"
remove_empty_dir "$HOST_SUPPORT_DIR"

echo ""
if [[ "$REMOVED" -eq 0 ]]; then
    echo -e "${YELLOW}Nothing found to remove.${RESET}"
else
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo -e "${GREEN}${BOLD}Dry run complete.${RESET} Re-run without --dry-run to delete."
    else
        echo -e "${GREEN}${BOLD}Done.${RESET} RemCTL files removed."
    fi
fi

echo ""
echo -e "${BOLD}Manual cleanup not performed:${RESET}"
echo -e "  ${DIM}- Shell config:${RESET} remove any remctl PATH or completion lines from ~/.zshrc if you added them."
echo -e "  ${DIM}- macOS permissions:${RESET} revoke Reminders, Automation, or Full Disk Access in System Settings if desired."
echo -e "  ${DIM}- Shell cache:${RESET} run hash -r or open a new terminal."
echo -e "  ${DIM}- TCC database:${RESET} Full Disk Access entries for the Capability Host remain; remove manually in System Settings."
echo ""
