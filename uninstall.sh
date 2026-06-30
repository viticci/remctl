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
    remctl_serialization.py
    remctl_smart_lists.py
    remctl-bridge
    remctl-private
    remctl-permissions
    remctl-permissions-icon.png
    completions/_remctl
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

for bin_dir in "${BIN_DIRS[@]}"; do
    if [[ ! -d "$bin_dir" ]]; then
        echo -e "${DIM}Skipping missing $bin_dir${RESET}"
        continue
    fi
    echo -e "${BLUE}->${RESET} Checking $bin_dir"
    for file in "${FILES[@]}"; do
        remove_file "$bin_dir/$file"
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
echo ""
