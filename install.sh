#!/bin/bash
# RemCTL Installer
# Installs remctl, remctl-bridge, remctl-private, remctl-permissions, and shared runtime helpers.

set -euo pipefail

BOOTSTRAP=0
RUN_DOCTOR=0
COMPLETION_SHELL="auto"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --bootstrap                 Install completions and create config for first-run onboarding
  --doctor                    Run `remctl doctor` after installation
  --shell-completions SHELL   Install completions for auto, zsh, bash, fish, or none (default: auto)
  -h, --help                  Show this help text

Notes:
  The installer copies binaries into ~/bin by default.
  Use PREFIX="$HOME/.local" if you want ~/.local/bin instead.
  remctl-private is optional and only used by explicit --private writes.
  Run `remctl onboard`, then `remctl permissions full-disk-access` for EventKit and database access.
  Run `remctl doctor` after permissions, or pass --doctor when upgrading an already-authorized install.
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

echo -e "${BLUE}→${RESET} Installing shared runtime helpers..."
cp "$SCRIPT_DIR/remctl_runtime.py" "$BIN_DIR/remctl_runtime.py"
chmod 644 "$BIN_DIR/remctl_runtime.py"
echo -e "  ${GREEN}✓${RESET} remctl_runtime.py → $BIN_DIR/remctl_runtime.py"

echo -e "${BLUE}→${RESET} Installing shared serialization helpers..."
cp "$SCRIPT_DIR/remctl_serialization.py" "$BIN_DIR/remctl_serialization.py"
chmod 644 "$BIN_DIR/remctl_serialization.py"
echo -e "  ${GREEN}✓${RESET} remctl_serialization.py → $BIN_DIR/remctl_serialization.py"

echo -e "${BLUE}→${RESET} Installing smart list helpers..."
cp "$SCRIPT_DIR/remctl_smart_lists.py" "$BIN_DIR/remctl_smart_lists.py"
chmod 644 "$BIN_DIR/remctl_smart_lists.py"
echo -e "  ${GREEN}✓${RESET} remctl_smart_lists.py → $BIN_DIR/remctl_smart_lists.py"

echo -e "${BLUE}→${RESET} Installing zsh completion source..."
mkdir -p "$BIN_DIR/completions"
completion_tmp="$BIN_DIR/completions/_remctl.tmp"
"$BIN_DIR/remctl" completion zsh > "$completion_tmp"
mv "$completion_tmp" "$BIN_DIR/completions/_remctl"
chmod 644 "$BIN_DIR/completions/_remctl"
echo -e "  ${GREEN}✓${RESET} _remctl → $BIN_DIR/completions/_remctl"

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

# 3. Check PATH
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
fi
echo ""
