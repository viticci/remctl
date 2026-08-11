import plistlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_helper_plist_declares_narrow_identity_and_permissions():
    with (ROOT / "agent-helper" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleIdentifier"] == "com.viticci.remctl.agent-helper"
    assert info["LSUIElement"] is True
    assert info["NSAppleScriptEnabled"] is True
    assert info["OSAScriptingDefinition"] == "RemCTLAgentHelper.sdef"
    assert info["NSRemindersFullAccessUsageDescription"]
    assert info["NSAppleEventsUsageDescription"]


def test_agent_helper_sources_do_not_embed_a_user_home_path():
    paths = [
        ROOT / "agent-helper" / "RemCTLAgentHelper.swift",
        ROOT / "agent-helper" / "remctl-agent.swift",
        ROOT / "scripts" / "install-agent-helper.sh",
        ROOT / "scripts" / "uninstall-agent-helper.sh",
    ]
    for path in paths:
        text = path.read_text()
        assert "/Users/" not in text
        assert "wousp" not in text.lower()


def test_agent_helper_installer_help_is_side_effect_free():
    completed = subprocess.run(
        [str(ROOT / "scripts" / "install-agent-helper.sh"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "REMCTL_CODESIGN_IDENTITY" in completed.stdout
    assert "Full Disk Access" in completed.stdout


def test_agent_helper_transport_uses_apple_events_not_a_socket():
    helper = (ROOT / "agent-helper" / "RemCTLAgentHelper.swift").read_text()
    client = (ROOT / "agent-helper" / "remctl-agent.swift").read_text()
    assert "NSAppleEventManager" in helper
    assert "/usr/bin/osascript" in client
    assert "item 1 of argv" in client
    assert "socket" not in helper.lower()
    assert "socket" not in client.lower()


def test_agent_helper_uninstaller_dry_run_is_safe(tmp_path):
    app_parent = tmp_path / "Applications"
    bin_dir = tmp_path / "bin"
    app = app_parent / "RemCTL Agent Helper.app"
    client = bin_dir / "remctl-agent"
    app.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    client.touch()
    completed = subprocess.run(
        [str(ROOT / "scripts" / "uninstall-agent-helper.sh"), "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path),
            "REMCTL_BIN_DIR": str(bin_dir),
            "REMCTL_AGENT_APP_DIR": str(app_parent),
        },
    )
    assert f"would remove {app}" in completed.stdout
    assert f"would remove {client}" in completed.stdout
    assert app.exists()
    assert client.exists()
