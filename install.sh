#!/bin/bash
# RemCTL Installer
# Installs remctl, remctl-bridge, remctl-server, and shared runtime helpers.

set -euo pipefail

BOOTSTRAP=0
WITH_SERVICE=0
RUN_DOCTOR=0
COMPLETION_SHELL="auto"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --bootstrap                 Install completions and run doctor
  --with-service              Install and start the launch agent after copying binaries
  --doctor                    Run `remctl doctor` after installation
  --shell-completions SHELL   Install completions for auto, zsh, bash, fish, or none (default: auto)
  -h, --help                  Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bootstrap)
            BOOTSTRAP=1
            RUN_DOCTOR=1
            shift
            ;;
        --with-service)
            WITH_SERVICE=1
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

echo ""
echo -e "${GREEN}██████${YELLOW}██████${ORANGE:-\033[38;2;245;132;31m}█████${RED}██████${RESET}"
echo -e "${BOLD}RemCTL Installer${RESET}"
echo ""

# Ensure ~/bin exists
mkdir -p "$BIN_DIR"
mkdir -p "$CONFIG_DIR"

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

echo -e "${BLUE}→${RESET} Installing zsh completion source..."
mkdir -p "$BIN_DIR/completions"
cp "$SCRIPT_DIR/completions/_remctl" "$BIN_DIR/completions/_remctl"
chmod 644 "$BIN_DIR/completions/_remctl"
echo -e "  ${GREEN}✓${RESET} _remctl → $BIN_DIR/completions/_remctl"

# 2. Compile and install Swift bridge
echo -e "${BLUE}→${RESET} Compiling remctl-bridge (Swift/EventKit)..."
if command -v swiftc &>/dev/null; then
    swiftc -O \
        -framework EventKit \
        -framework Foundation \
        -o "$BIN_DIR/remctl-bridge" \
        "$SCRIPT_DIR/remctl-bridge.swift" 2>/dev/null
    chmod +x "$BIN_DIR/remctl-bridge"
    echo -e "  ${GREEN}✓${RESET} remctl-bridge → $BIN_DIR/remctl-bridge"
else
    echo -e "  ${YELLOW}⚠${RESET} swiftc not found — install Xcode Command Line Tools"
    echo -e "    ${DIM}Run: xcode-select --install${RESET}"
    echo -e "    ${DIM}remctl will fall back to AppleScript for writes${RESET}"
fi

# 3. Install API server
echo -e "${BLUE}→${RESET} Installing remctl-server..."
cp "$SCRIPT_DIR/remctl-server" "$BIN_DIR/remctl-server"
chmod +x "$BIN_DIR/remctl-server"
echo -e "  ${GREEN}✓${RESET} remctl-server → $BIN_DIR/remctl-server"

# 5. Generate API token if missing
if [[ ! -f "$CONFIG_DIR/api-token" ]]; then
    TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    echo "$TOKEN" > "$CONFIG_DIR/api-token"
    chmod 600 "$CONFIG_DIR/api-token"
    echo -e "  ${GREEN}✓${RESET} API token generated → $CONFIG_DIR/api-token"
fi

# 6. Check PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo -e "${YELLOW}⚠${RESET}  $BIN_DIR is not in your PATH. Add it:"
    echo -e "  ${DIM}echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc${RESET}"
fi

SETUP_SHELL="$COMPLETION_SHELL"
if [[ "$SETUP_SHELL" == "none" ]]; then
    SETUP_SHELL="skip"
fi

if [[ "$SETUP_SHELL" != "skip" || "$WITH_SERVICE" -eq 1 ]]; then
    echo -e "${BLUE}→${RESET} Running remctl setup..."
    SETUP_ARGS=("$BIN_DIR/remctl" "setup" "--shell" "$SETUP_SHELL")
    if [[ "$WITH_SERVICE" -eq 1 ]]; then
        SETUP_ARGS+=("--service" "install")
    else
        SETUP_ARGS+=("--service" "skip")
    fi
    "${SETUP_ARGS[@]}"
fi

if [[ "$RUN_DOCTOR" -eq 1 || "$BOOTSTRAP" -eq 1 ]]; then
    echo -e "${BLUE}→${RESET} Running remctl doctor..."
    "$BIN_DIR/remctl" doctor
fi

echo ""
echo -e "${GREEN}${BOLD}Done!${RESET} RemCTL v3.0.0 installed."
if [[ "$BOOTSTRAP" -eq 1 ]]; then
    echo -e "${DIM}Run 'remctl today' to verify the CLI against your reminders.${RESET}"
else
    echo -e "${DIM}Run 'remctl setup --doctor' to finish setup and check your environment.${RESET}"
fi
echo ""
