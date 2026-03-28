#!/bin/bash
# RemCTL Installer
# Installs remctl, remctl-bridge, and remctl-server to ~/bin

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/bin"
CONFIG_DIR="$HOME/.config/remctl"

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

# 4. Install zsh completions
if [[ -d "/usr/local/share/zsh/site-functions" ]] || [[ -d "$HOME/.zsh/completions" ]]; then
    COMP_DIR="${HOME}/.zsh/completions"
    mkdir -p "$COMP_DIR"
    cp "$SCRIPT_DIR/completions/_remctl" "$COMP_DIR/_remctl" 2>/dev/null || true
    echo -e "  ${GREEN}✓${RESET} zsh completions → $COMP_DIR/_remctl"
fi

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
    echo -e "  ${DIM}echo 'export PATH=\"\$HOME/bin:\$PATH\"' >> ~/.zshrc${RESET}"
fi

echo ""
echo -e "${GREEN}${BOLD}Done!${RESET} RemCTL v3.0.0 installed."
echo -e "${DIM}Run 'remctl' to get started.${RESET}"
echo ""
