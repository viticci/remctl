#!/bin/bash
# Removes only artifacts whose identity matches the RemCTL installer contract.

set -euo pipefail

DRY_RUN=0
KEEP_CONFIG=0
usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [options]

Options:
  --dry-run       Print what would be stopped and removed
  --keep-config   Keep the RemCTL config directory
  -h, --help      Show this help text

PREFIX, REMCTL_BIN_DIR, REMCTL_APP_DIR, and REMCTL_LAUNCH_AGENT_DIR select
the same destinations as install.sh. Full Disk Access, Reminders, and Automation
grants for the signed RemCTL Capability Host are left unchanged.
EOF
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --keep-config) KEEP_CONFIG=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

RED='\033[38;2;224;47;55m'; GREEN='\033[38;2;97;187;70m'; YELLOW='\033[38;2;253;181;21m'
BLUE='\033[38;2;0;157;220m'; DIM='\033[2m'; BOLD='\033[1m'; RESET='\033[0m'
fail() { echo -e "${RED}ERROR:${RESET} $*" >&2; exit 1; }

PREFIX_WAS_SET=0
if [[ -n "${PREFIX+x}" ]]; then PREFIX_WAS_SET=1; fi
PREFIX="${PREFIX:-$HOME}"
APP_DIR="${REMCTL_APP_DIR:-$PREFIX/Applications}"
LAUNCH_AGENT_DIR="${REMCTL_LAUNCH_AGENT_DIR:-$HOME/Library/LaunchAgents}"
APP_PATH="$APP_DIR/RemCTL Capability Host.app"
HOST_EXECUTABLE="$APP_PATH/Contents/MacOS/RemCTL Capability Host"
AGENT_LABEL="net.macstories.remctl.capability-host"
AGENT_PATH="$LAUNCH_AGENT_DIR/$AGENT_LABEL.plist"
SOCKET_PATH="$PREFIX/Library/Application Support/RemCTL/capability-host.sock"
SUPPORT_DIR="$(dirname "$SOCKET_PATH")"
CONFIG_BASE="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_DIR="${REMCTL_CONFIG_DIR:-$CONFIG_BASE/remctl}"
SKIP_LAUNCHSERVICES="${REMCTL_SKIP_LAUNCHSERVICES:-0}"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

if [[ -n "${REMCTL_BIN_DIR:-}" ]]; then
    BIN_DIRS=("$REMCTL_BIN_DIR")
elif [[ "$PREFIX_WAS_SET" == "1" ]]; then
    BIN_DIRS=("$PREFIX/bin")
else
    BIN_DIRS=("$HOME/bin" "$HOME/.local/bin")
fi

if [[ "$SKIP_LAUNCHSERVICES" == "1" && "$PREFIX" == "$HOME" ]]; then
    fail "REMCTL_SKIP_LAUNCHSERVICES is allowed only with a non-home PREFIX."
fi
if [[ "$SKIP_LAUNCHSERVICES" == "1" ]]; then
    case "$LAUNCH_AGENT_DIR" in
        "$PREFIX"/*) ;;
        *) fail "Temp-prefix simulation requires a LaunchAgent directory under PREFIX; set REMCTL_LAUNCH_AGENT_DIR." ;;
    esac
fi

safe_config_dir() {
    [[ -n "$CONFIG_DIR" && "$CONFIG_DIR" == /* && "$CONFIG_DIR" != "/" && "$CONFIG_DIR" != "$HOME" && "$CONFIG_DIR" != "$CONFIG_BASE" ]]
    [[ "$(basename "$CONFIG_DIR")" == "remctl" ]]
}
if [[ "$KEEP_CONFIG" != "1" ]]; then
    safe_config_dir || fail "Refusing suspicious config path before uninstall: $CONFIG_DIR"
fi

plist_value() {
    /usr/libexec/PlistBuddy -c "Print :$2" "$1" 2>/dev/null || true
}

agent_owned() {
    [[ -f "$AGENT_PATH" && ! -L "$AGENT_PATH" ]] || return 1
    [[ "$(plist_value "$AGENT_PATH" Label)" == "$AGENT_LABEL" ]] || return 1
    local arguments
    arguments="$(plutil -extract ProgramArguments json -o - "$AGENT_PATH" 2>/dev/null || true)"
    /usr/bin/python3 -c 'import json,sys; raise SystemExit(0 if json.loads(sys.argv[1]) == sys.argv[2:] else 1)' \
        "$arguments" "$HOST_EXECUTABLE" --run-capability-host --socket "$SOCKET_PATH" >/dev/null 2>&1 || return 1
}

app_owned() {
    [[ -d "$APP_PATH" && ! -L "$APP_PATH" ]] || return 1
    local info="$APP_PATH/Contents/Info.plist"
    [[ -f "$info" && ! -L "$info" ]] || return 1
    [[ "$(plist_value "$info" CFBundleIdentifier)" == "$AGENT_LABEL" ]] || return 1
    [[ "$(plist_value "$info" CFBundleExecutable)" == "RemCTL Capability Host" ]] || return 1
    [[ -f "$HOST_EXECUTABLE" && ! -L "$HOST_EXECUTABLE" && -x "$HOST_EXECUTABLE" ]] || return 1
    /usr/bin/codesign --verify --deep --strict "$APP_PATH" >/dev/null 2>&1 || return 1
    local resources="$APP_PATH/Contents/Resources"
    local runtime="$resources/CapabilityRuntime"
    for required in \
        "$resources/remctl-capability-python-path" \
        "$resources/remctl-capability-host-socket-path" \
        "$resources/remctl-capability-host-launch-agent-path" \
        "$resources/remctl-capability-runtime.json" \
        "$runtime/bin/remctl-bridge" \
        "$runtime/bin/remctl-private"
    do
        [[ -f "$required" && ! -L "$required" ]] || return 1
    done
    [[ -x "$runtime/bin/remctl-bridge" && -x "$runtime/bin/remctl-private" ]] || return 1
    [[ "$(sed -n '1p' "$resources/remctl-capability-host-socket-path")" == "$SOCKET_PATH" ]] || return 1
    [[ "$(sed -n '1p' "$resources/remctl-capability-host-launch-agent-path")" == "$AGENT_PATH" ]] || return 1
}

bin_owned() {
    local bin_dir="$1"
    /usr/bin/python3 -I -S - "$bin_dir" <<'PY'
import hashlib,json,os,stat,sys
root=sys.argv[1]; manifest=os.path.join(root,".remctl-install-manifest.json")
managed={"remctl","remctl_runtime.py","remctl_images.py","remctl_serialization.py","remctl_smart_lists.py","remctl_broker.py","remctl_capability_policy.py","remctl_capabilities.py","remctl_mcp.py","remctl_mcp_widget.html","remctl-mcp-icon.png","remctl-mcp-icon-512.png","remctl-bridge","remctl-private","remctl-permissions","remctl-permissions-icon.png",".remctl-capability-host-app",".remctl-capability-host-signing-identity","completions/_remctl","completions/_rctl","completions/_reminders","rctl","reminders"}
def present(): return {n for n in managed if os.path.lexists(os.path.join(root,n))}
def match(name,item):
 p=os.path.join(root,name)
 try: m=os.lstat(p)
 except OSError:return False
 if item.get("type")=="symlink": return stat.S_ISLNK(m.st_mode) and os.readlink(p)==item.get("target")
 if item.get("type")!="file" or not stat.S_ISREG(m.st_mode) or stat.S_ISLNK(m.st_mode):return False
 with open(p,"rb") as h:d=hashlib.sha256(h.read()).hexdigest()
 return d==item.get("sha256")
if os.path.lexists(manifest):
 try:
  m=os.lstat(manifest)
  if (not stat.S_ISREG(m.st_mode) or stat.S_ISLNK(m.st_mode) or m.st_uid!=os.getuid() or stat.S_IMODE(m.st_mode)!=0o600):raise ValueError
  value=json.load(open(manifest,encoding="utf-8")); entries=value["entries"]
  if not isinstance(entries,dict):raise ValueError
  valid=value.get("version")==1 and all(isinstance(item,dict) for item in entries.values())
  valid=valid and not(set(entries)-managed) and present()==set(entries)
  valid=valid and all(match(n,item) for n,item in entries.items())
 except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):valid=False
 raise SystemExit(0 if valid else 1)
p=present()
legacy={"remctl":"82f71a03c10922e7f68601bc6d9e8300d394495330abe2e933d137b5405090dd","remctl_runtime.py":"051cdf8c72134f185f922de6489c3590d7efadfdfcc0158151c8b0508c2eee40","remctl_images.py":"c25523c5e8dca106bd122103b497b653527fa6a2a8e2bc49c57a5549f5782359","remctl_serialization.py":"4a23f4e8b1f5996de9a25788bce41baaf07e0efbb390a05d9ac6be04c8f6d6d9","remctl_smart_lists.py":"81a500c42712e1519c5af15244ec9b55483617aef3ca6a931c800d70fc2e29ee","completions/_remctl":"09539986de7736caeac55741d13281426c79639c8df02b552fc48207aa585c37","completions/_rctl":"09539986de7736caeac55741d13281426c79639c8df02b552fc48207aa585c37","completions/_reminders":"09539986de7736caeac55741d13281426c79639c8df02b552fc48207aa585c37"}
allowed=set(legacy)|{"remctl-bridge","remctl-private","remctl-permissions","remctl-permissions-icon.png","rctl","reminders"}
valid=bool(p) and p<=allowed and set(legacy)<=p
for n,d in legacy.items():valid &= match(n,{"type":"file","sha256":d})
for n in ("rctl","reminders"):valid &= match(n,{"type":"symlink","target":"remctl"})
for n in p&{"remctl-bridge","remctl-private","remctl-permissions"}:
 m=os.lstat(os.path.join(root,n)); valid &= stat.S_ISREG(m.st_mode) and not stat.S_ISLNK(m.st_mode) and os.access(os.path.join(root,n),os.X_OK)
 with open(os.path.join(root,n),"rb") as h:valid &= h.read(4) in (b"\xcf\xfa\xed\xfe",b"\xfe\xed\xfa\xcf")
if p&{"remctl-bridge","remctl-private","remctl-permissions","remctl-permissions-icon.png"}:valid=False
raise SystemExit(0 if valid else 1)
PY
}

job_loaded() {
    launchctl print "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1
}

stop_job() {
    if job_loaded; then launchctl bootout "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1 || true; fi
    for _ in {1..50}; do job_loaded || return 0; sleep 0.1; done
    return 1
}

socket_owned() {
    /usr/bin/python3 -I -S - "$SOCKET_PATH" <<'PY'
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
    /usr/bin/python3 -I -S - "$SOCKET_PATH" <<'PY'
import errno, os, socket, stat, sys
path=sys.argv[1]; parent=os.path.dirname(path); name=os.path.basename(path)
directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
    parent_metadata=os.fstat(directory)
    first=os.stat(name,dir_fd=directory,follow_symlinks=False)
    valid=(os.path.realpath(parent) == parent and stat.S_ISDIR(parent_metadata.st_mode)
        and parent_metadata.st_uid == os.getuid() and stat.S_IMODE(parent_metadata.st_mode) == 0o700
        and stat.S_ISSOCK(first.st_mode) and first.st_uid == os.getuid()
        and stat.S_IMODE(first.st_mode) == 0o600)
    if not valid: raise SystemExit(1)
    probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    try:
        probe.settimeout(0.25); result=probe.connect_ex(path)
    finally: probe.close()
    if result == 0 or result not in (errno.ECONNREFUSED,errno.ENOENT): raise SystemExit(1)
    second=os.stat(name,dir_fd=directory,follow_symlinks=False)
    if (first.st_dev,first.st_ino,first.st_mode,first.st_uid) != (second.st_dev,second.st_ino,second.st_mode,second.st_uid): raise SystemExit(1)
    os.unlink(name,dir_fd=directory)
finally: os.close(directory)
PY
}

FILES=(
    remctl remctl_runtime.py remctl_images.py remctl_serialization.py remctl_smart_lists.py
    remctl_broker.py remctl_capability_policy.py remctl_capabilities.py remctl_mcp.py remctl_mcp_widget.html
    remctl-mcp-icon.png remctl-mcp-icon-512.png
    remctl-bridge remctl-private remctl-permissions remctl-permissions-icon.png
    .remctl-capability-host-app .remctl-capability-host-signing-identity
    .remctl-install-manifest.json
    completions/_remctl completions/_rctl completions/_reminders rctl reminders
)

# Recognize an unmigrated prefix-based agent only through the signed app marker.
LEGACY_AGENT_PATH="$PREFIX/Library/LaunchAgents/$AGENT_LABEL.plist"
if [[ -z "${REMCTL_LAUNCH_AGENT_DIR:-}" && "$AGENT_PATH" != "$LEGACY_AGENT_PATH" &&
      "$(cat "$APP_PATH/Contents/Resources/remctl-capability-host-launch-agent-path" 2>/dev/null || true)" == "$LEGACY_AGENT_PATH" ]]; then
    [[ ! -e "$AGENT_PATH" && ! -L "$AGENT_PATH" ]] || fail "Both old and new LaunchAgent paths exist; nothing was removed."
    AGENT_PATH="$LEGACY_AGENT_PATH"
    app_owned || fail "The legacy LaunchAgent does not match a valid signed installation."
    if [[ -e "$AGENT_PATH" || -L "$AGENT_PATH" ]]; then
        agent_owned || fail "The legacy LaunchAgent does not match a valid signed installation."
    fi
fi

# An interrupted install owns the recovery decision. Never delete either side.
check_backup() {
    [[ ! -e "$1.remctl-transaction-backup" && ! -L "$1.remctl-transaction-backup" ]] || \
        fail "Unresolved installer backup found: $1.remctl-transaction-backup. Re-run install.sh only after recovering that exact transaction."
}
check_backup "$APP_PATH"; check_backup "$AGENT_PATH"
for bin_dir in "${BIN_DIRS[@]}"; do
    for name in "${FILES[@]}"; do check_backup "$bin_dir/$name"; done
done

echo ""; echo -e "${BOLD}RemCTL Uninstaller${RESET}"
if [[ "$DRY_RUN" == "1" ]]; then echo -e "${YELLOW}(dry run; nothing will be changed)${RESET}"; fi
echo ""

# Validate every structured or special artifact before deleting simpler files.
# This makes a foreign app, plist, or socket a pre-write failure.
OWNED_BIN_COUNT=0
for bin_dir in "${BIN_DIRS[@]}"; do
    if bin_owned "$bin_dir"; then OWNED_BIN_COUNT=$((OWNED_BIN_COUNT + 1)); fi
done
if [[ "$OWNED_BIN_COUNT" -eq 0 ]]; then
    if [[ -e "$APP_PATH" || -L "$APP_PATH" || -e "$AGENT_PATH" || -L "$AGENT_PATH" || -e "$SOCKET_PATH" || -L "$SOCKET_PATH" ]]; then
        fail "Capability-host artifacts exist without an exact installer ownership marker; nothing was removed."
    fi
fi
if [[ -e "$AGENT_PATH" || -L "$AGENT_PATH" ]]; then
    agent_owned || fail "Refusing to remove a LaunchAgent that does not match the RemCTL contract: $AGENT_PATH"
fi
if [[ -e "$APP_PATH" || -L "$APP_PATH" ]]; then
    app_owned || fail "Refusing to remove an app that does not match the RemCTL contract: $APP_PATH"
fi
if [[ -e "$SOCKET_PATH" || -L "$SOCKET_PATH" ]]; then
    socket_owned || fail "Refusing to remove a foreign or unsafe socket object at $SOCKET_PATH"
fi
if [[ "$SKIP_LAUNCHSERVICES" != "1" && -d "$APP_PATH" ]]; then
    [[ -x "$LSREGISTER" ]] || fail "LaunchServices registration tool is unavailable; nothing was changed."
fi

# Never stop a job unless its installed plist proves the exact executable and
# socket contract. Never publish removals until launchd proves the job stopped.
if [[ "$SKIP_LAUNCHSERVICES" != "1" ]] && job_loaded; then
    agent_owned || fail "The loaded $AGENT_LABEL job has no exact installer-owned plist; refusing to stop it."
    if [[ "$DRY_RUN" == "1" ]]; then
        echo -e "  ${YELLOW}would stop${RESET} $AGENT_LABEL"
    else
        echo -e "${BLUE}→${RESET} Stopping $AGENT_LABEL..."
        stop_job || fail "The capability host did not stop; no files were removed."
    fi
fi

REMOVED=0
if [[ "$SKIP_LAUNCHSERVICES" != "1" && -d "$APP_PATH" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
        echo -e "  ${YELLOW}would unregister${RESET} $APP_PATH"
    else
        "$LSREGISTER" -u "$APP_PATH" >/dev/null || fail "Could not unregister the capability host. The service was stopped, but no files or socket were removed."
    fi
fi

if [[ -e "$SOCKET_PATH" || -L "$SOCKET_PATH" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
        echo -e "  ${YELLOW}would remove stopped socket${RESET} $SOCKET_PATH"
    else
        safe_remove_socket || fail "The socket is still live or changed identity after service stop; no files were removed."
        echo -e "  ${GREEN}removed${RESET} $SOCKET_PATH"
    fi
    REMOVED=$((REMOVED + 1))
fi

remove_file() {
    local path="$1"
    if [[ -e "$path" || -L "$path" ]]; then
        if [[ -d "$path" && ! -L "$path" ]]; then
            echo -e "  ${DIM}left unexpected directory${RESET} $path"
            return
        fi
        if [[ "$DRY_RUN" == "1" ]]; then echo -e "  ${YELLOW}would remove${RESET} $path"
        else rm -f -- "$path"; echo -e "  ${GREEN}removed${RESET} $path"; fi
        REMOVED=$((REMOVED + 1))
    fi
}
remove_empty_dir() {
    local path="$1"
    if [[ -d "$path" && ! -L "$path" ]]; then
        if [[ -z "$(find "$path" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
            if [[ "$DRY_RUN" == "1" ]]; then echo -e "  ${YELLOW}would remove empty dir${RESET} $path"
            else rmdir -- "$path"; echo -e "  ${GREEN}removed empty dir${RESET} $path"; fi
            REMOVED=$((REMOVED + 1))
        fi
    fi
}
remove_alias() {
    local path="$1"
    if [[ -L "$path" ]]; then
        local target; target="$(readlink "$path")"
        if [[ "$target" == "remctl" ]]; then remove_file "$path"
        else echo -e "  ${DIM}left unrelated symlink${RESET} $path"; fi
    fi
}

for bin_dir in "${BIN_DIRS[@]}"; do
    bin_owned "$bin_dir" || continue
    [[ -d "$bin_dir" ]] || continue
    echo -e "${BLUE}→${RESET} Checking $bin_dir"
    for name in "${FILES[@]}"; do
        case "$name" in rctl|reminders|.remctl-install-manifest.json) continue ;; esac
        remove_file "$bin_dir/$name"
    done
    for module in remctl_runtime remctl_images remctl_serialization remctl_smart_lists remctl_broker remctl_capability_policy remctl_capabilities remctl_mcp; do
        for cache_path in "$bin_dir/__pycache__/${module}.cpython-"*.pyc; do
            [[ -e "$cache_path" || -L "$cache_path" ]] && remove_file "$cache_path"
        done
    done
    remove_alias "$bin_dir/rctl"; remove_alias "$bin_dir/reminders"
    remove_empty_dir "$bin_dir/completions"
    remove_empty_dir "$bin_dir/__pycache__"
    # Keep ownership evidence until every artifact it describes is gone.
    remove_file "$bin_dir/.remctl-install-manifest.json"
done

if [[ -e "$AGENT_PATH" || -L "$AGENT_PATH" ]]; then
    agent_owned || fail "Refusing to remove a LaunchAgent that does not match the RemCTL contract: $AGENT_PATH"
    remove_file "$AGENT_PATH"
fi

if [[ -e "$APP_PATH" || -L "$APP_PATH" ]]; then
    app_owned || fail "Refusing to remove an app that does not match the RemCTL contract: $APP_PATH"
    if [[ "$DRY_RUN" == "1" ]]; then echo -e "  ${YELLOW}would remove${RESET} $APP_PATH"
    else rm -rf -- "$APP_PATH"; echo -e "  ${GREEN}removed${RESET} $APP_PATH"; fi
    REMOVED=$((REMOVED + 1))
fi

remove_empty_dir "$SUPPORT_DIR"

if [[ "$OWNED_BIN_COUNT" -eq 0 ]]; then
    [[ -d "$CONFIG_DIR" ]] && echo -e "${DIM}Keeping unowned config directory: $CONFIG_DIR${RESET}"
elif [[ "$KEEP_CONFIG" == "1" ]]; then
    echo -e "${DIM}Keeping config directory: $CONFIG_DIR${RESET}"
elif [[ -d "$CONFIG_DIR" ]]; then
    safe_config_dir || fail "Refusing to remove suspicious config path: $CONFIG_DIR"
    if [[ "$DRY_RUN" == "1" ]]; then echo -e "  ${YELLOW}would remove config dir${RESET} $CONFIG_DIR"
    else rm -rf -- "$CONFIG_DIR"; echo -e "  ${GREEN}removed config dir${RESET} $CONFIG_DIR"; fi
    REMOVED=$((REMOVED + 1))
fi

echo ""
if [[ "$DRY_RUN" == "1" ]]; then echo -e "${GREEN}${BOLD}Dry run complete.${RESET}"
elif [[ "$REMOVED" == "0" ]]; then echo -e "${YELLOW}Nothing found to remove.${RESET}"
else echo -e "${GREEN}${BOLD}Done.${RESET} RemCTL-owned files and service artifacts removed."; fi
echo -e "${DIM}The signed host's Full Disk Access, Reminders, and Automation grants were not reset. Revoke them in System Settings if desired.${RESET}"
