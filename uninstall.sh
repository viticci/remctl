#!/bin/bash
# RemCTL Uninstaller
# Removes remctl, remctl-bridge, remctl-private, remctl-permissions, shared
# runtime helpers, zsh completion, and the config directory.
#
# Mirrors install.sh: it only deletes the exact files the installer created,
# in both ~/bin and ~/.local/bin by default.

set -euo pipefail

DRY_RUN=0
KEEP_CONFIG=0

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [options]

Options:
  --dry-run        Show what would be removed without deleting anything
  --keep-config    Do not remove the RemCTL config directory
  -h, --help       Show this help text

Notes:
  By default this checks BOTH ~/bin and ~/.local/bin.
  Override the target with PREFIX="$HOME/.local" or REMCTL_BIN_DIR=/custom/bin
  to uninstall from a single location only.
  Config dir defaults to ${XDG_CONFIG_HOME:-~/.config}/remctl.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=1; shift ;;
        --keep-config) KEEP_CONFIG=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

# Colors
RED='\033[38;2;224;47;55m'
GREEN='\033[38;2;97;187;70m'
YELLOW='\033[38;2;253;181;21m'
BLUE='\033[38;2;0;157;220m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

# Files the installer copies/compiles into the bin dir.
FILES=(
    remctl
    remctl_runtime.py
    remctl_serialization.py
    remctl_smart_lists.py
    remctl-bridge
    remctl-permissions
    remctl-permissions-icon.png
    remctl-private
    completions/_remctl
)

# Determine which bin dirs to clean.
# If PREFIX or REMCTL_BIN_DIR is set, honor it (single target, same as installer).
# Otherwise default to scanning both common locations.
if [[ -n "${REMCTL_BIN_DIR:-}" ]]; then
    BIN_DIRS=("$REMCTL_BIN_DIR")
elif [[ -n "${PREFIX:-}" ]]; then
    BIN_DIRS=("$PREFIX/bin")
else
    BIN_DIRS=("$HOME/bin" "$HOME/.local/bin")
fi

CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_DIR="${REMCTL_CONFIG_DIR:-$CONFIG_BASE/remctl}"

echo ""
echo -e "${BOLD}RemCTL Uninstaller${RESET}"
[[ "$DRY_RUN" -eq 1 ]] && echo -e "${YELLOW}(dry run — nothing will be deleted)${RESET}"
echo ""

REMOVED=0

remove_path() {
    local target="$1"
    if [[ -e "$target" || -L "$target" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo -e "  ${YELLOW}would remove${RESET} $target"
        else
            rm -f "$target"
            echo -e "  ${GREEN}✓${RESET} removed $target"
        fi
        REMOVED=$((REMOVED + 1))
    fi
}

# 1. Remove installed files from each bin dir
for BIN_DIR in "${BIN_DIRS[@]}"; do
    [[ -d "$BIN_DIR" ]] || continue
    echo -e "${BLUE}→${RESET} Checking $BIN_DIR ..."
    for f in "${FILES[@]}"; do
        remove_path "$BIN_DIR/$f"
    done

    # Remove the completions dir only if it is now empty
    if [[ -d "$BIN_DIR/completions" ]]; then
        if [[ -z "$(ls -A "$BIN_DIR/completions" 2>/dev/null)" ]]; then
            if [[ "$DRY_RUN" -eq 1 ]]; then
                echo -e "  ${YELLOW}would remove empty dir${RESET} $BIN_DIR/completions"
            else
                rmdir "$BIN_DIR/completions" 2>/dev/null \
                    && echo -e "  ${GREEN}✓${RESET} removed empty dir $BIN_DIR/completions" || true
            fi
        else
            echo -e "  ${DIM}left $BIN_DIR/completions (not empty)${RESET}"
        fi
    fi
done

# 2. Remove config directory
if [[ "$KEEP_CONFIG" -eq 0 ]]; then
    if [[ -d "$CONFIG_DIR" ]]; then
        echo -e "${BLUE}→${RESET} Removing config directory ..."
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo -e "  ${YELLOW}would remove${RESET} $CONFIG_DIR"
        else
            rm -rf "$CONFIG_DIR"
            echo -e "  ${GREEN}✓${RESET} removed $CONFIG_DIR"
        fi
        REMOVED=$((REMOVED + 1))
    fi
else
    echo -e "${DIM}Keeping config directory: $CONFIG_DIR${RESET}"
fi

echo ""
if [[ "$REMOVED" -eq 0 ]]; then
    echo -e "${YELLOW}Nothing found to remove.${RESET} RemCTL may already be uninstalled, or it was installed to a custom location."
    echo -e "${DIM}Try: PREFIX=\"/your/prefix\" ./uninstall.sh${RESET}"
else
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo -e "${GREEN}${BOLD}Dry run complete.${RESET} Re-run without --dry-run to delete."
    else
        echo -e "${GREEN}${BOLD}Done!${RESET} RemCTL files removed."
    fi
fi

# 3. Things this script cannot safely auto-remove
echo ""
echo -e "${BOLD}Manual cleanup (not done automatically):${RESET}"
echo -e "  ${DIM}• Shell config:${RESET} remctl setup may have added completion / PATH lines."
echo -e "    Check ~/.zshrc for 'remctl' references and any 'export PATH=...bin' line you added."
echo -e "  ${DIM}• macOS permissions:${RESET} revoke under System Settings → Privacy & Security"
echo -e "    (Reminders, Automation, Full Disk Access) if you want a fully clean slate."
echo -e "  ${DIM}Then run:${RESET} hash -r   ${DIM}(or open a new Terminal)${RESET}"
echo ""
