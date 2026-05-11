from __future__ import annotations

from datetime import datetime
import tempfile
import unittest
import stat
from pathlib import Path
from unittest import mock

import remctl_runtime


class RuntimeTests(unittest.TestCase):
    def test_resolve_store_dir_honors_override(self):
        with mock.patch.dict("os.environ", {"REMCTL_STORE_DIR": "/tmp/reminders-store"}, clear=False):
            self.assertEqual(remctl_runtime.resolve_store_dir(), Path("/tmp/reminders-store"))

    def test_resolve_config_dir_prefers_remctl_override(self):
        with mock.patch.dict("os.environ", {"REMCTL_CONFIG_DIR": "/tmp/remctl-config"}, clear=False):
            self.assertEqual(remctl_runtime.resolve_config_dir(), Path("/tmp/remctl-config"))

    def test_resolve_config_dir_uses_xdg(self):
        with mock.patch.dict(
            "os.environ",
            {"XDG_CONFIG_HOME": "/tmp/xdg-home"},
            clear=True,
        ):
            self.assertEqual(
                remctl_runtime.resolve_config_dir(),
                Path("/tmp/xdg-home/remctl"),
            )

    def test_resolve_binary_path_prefers_env_override(self):
        with mock.patch.dict("os.environ", {"REMCTL_BRIDGE_PATH": "/tmp/custom-bridge"}, clear=False):
            resolved = remctl_runtime.resolve_binary_path("/tmp/remctl", "remctl-bridge", "REMCTL_BRIDGE_PATH")
        self.assertEqual(resolved, Path("/tmp/custom-bridge"))

    def test_resolve_binary_path_finds_sibling_binary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "remctl"
            bridge_path = Path(tmpdir) / "remctl-bridge"
            script_path.write_text("#!/usr/bin/env python3\n")
            bridge_path.write_text("bridge")
            with mock.patch.dict("os.environ", {}, clear=True):
                resolved = remctl_runtime.resolve_binary_path(
                    str(script_path),
                    "remctl-bridge",
                    "REMCTL_BRIDGE_PATH",
                )
        self.assertEqual(resolved, bridge_path.resolve())

    def test_mask_secret_hides_middle(self):
        self.assertEqual(remctl_runtime.mask_secret("abcdefgh12345678"), "abcd...5678")
        self.assertEqual(remctl_runtime.mask_secret("short"), "*****")

    def test_write_private_text_file_uses_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config" / "api-token"
            remctl_runtime.write_private_text_file(path, "secret\n")
            self.assertEqual(path.read_text(), "secret\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_due_today_window_uses_start_of_day_bounds(self):
        now = datetime(2026, 4, 18, 14, 30, 0)
        sod, eod = remctl_runtime.due_today_window(now)
        self.assertEqual(sod, datetime(2026, 4, 18, 0, 0, 0))
        self.assertEqual(eod, datetime(2026, 4, 19, 0, 0, 0))

    def test_upcoming_window_starts_at_start_of_day(self):
        now = datetime(2026, 4, 18, 14, 30, 0)
        sod, end = remctl_runtime.upcoming_window(7, now)
        self.assertEqual(sod, datetime(2026, 4, 18, 0, 0, 0))
        self.assertEqual(end, datetime(2026, 4, 26, 0, 0, 0))

    def test_is_safe_remote_url_accepts_public_host(self):
        fake_addrinfo = [(None, None, None, None, ("93.184.216.34", 443))]
        with mock.patch("socket.getaddrinfo", return_value=fake_addrinfo):
            self.assertTrue(remctl_runtime.is_safe_remote_url("https://example.com"))

    def test_is_safe_remote_url_rejects_private_or_local_targets(self):
        cases = [
            ("http://localhost", [(None, None, None, None, ("127.0.0.1", 80))]),
            ("https://internal.example", [(None, None, None, None, ("10.0.0.8", 443))]),
            ("file:///tmp/test", []),
        ]
        for url, addrinfo in cases:
            with self.subTest(url=url):
                patcher = mock.patch("socket.getaddrinfo", return_value=addrinfo)
                if url.startswith("file:"):
                    patcher = mock.patch("socket.getaddrinfo")
                with patcher:
                    self.assertFalse(remctl_runtime.is_safe_remote_url(url))


if __name__ == "__main__":
    unittest.main()
