from __future__ import annotations

import plistlib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SWIFT_SOURCE = ROOT / "remctl-capability-host.swift"
INFO_PLIST = ROOT / "remctl-capability-host-Info.plist"
LAUNCH_AGENT_PLIST = ROOT / "remctl-read-broker-launchagent.plist"


class CapabilityHostContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.swift = SWIFT_SOURCE.read_text(encoding="utf-8")
        cls.info = plistlib.loads(INFO_PLIST.read_bytes())
        cls.launch_agent = plistlib.loads(LAUNCH_AGENT_PLIST.read_bytes())

    def test_info_plist_uses_stable_background_bundle_identifier(self):
        self.assertEqual(
            self.info["CFBundleIdentifier"],
            "net.macstories.remctl.capability-host",
        )
        self.assertTrue(self.info["LSBackgroundOnly"])
        self.assertEqual(self.info["CFBundleExecutable"], "remctl-capability-host")

    def test_launch_agent_uses_stable_label_and_fixed_role_arguments(self):
        self.assertEqual(
            self.launch_agent["Label"],
            "net.macstories.remctl.read-broker",
        )
        self.assertEqual(
            self.launch_agent["ProgramArguments"],
            [
                "__REMCTL_CAPABILITY_HOST_EXECUTABLE__",
                "--run-read-broker",
                "--socket",
                "__REMCTL_READ_BROKER_SOCKET__",
            ],
        )
        self.assertEqual(self.launch_agent["ProcessType"], "Background")

    def test_swift_source_rejects_unconfigured_sealed_placeholders(self):
        self.assertIn(
            'private let unconfiguredSentinelPrefix = "__REMCTL_"',
            self.swift,
        )
        self.assertIn(
            'private let configuredPythonExecutable = "__REMCTL_CAPABILITY_HOST_PYTHON__"',
            self.swift,
        )
        self.assertIn(
            'private let configuredBrokerEntrypoint = "__REMCTL_READ_BROKER_ENTRYPOINT__"',
            self.swift,
        )
        self.assertIn(
            'private let configuredManifestPath = "__REMCTL_CAPABILITY_HOST_MANIFEST__"',
            self.swift,
        )
        self.assertIn(
            'private let configuredManifestDigest = "__REMCTL_CAPABILITY_HOST_MANIFEST_DIGEST__"',
            self.swift,
        )
        self.assertIn("!looksUnconfigured(configuredPythonExecutable)", self.swift)
        self.assertIn("!looksUnconfigured(configuredBrokerEntrypoint)", self.swift)
        self.assertIn("!looksUnconfigured(configuredManifestPath)", self.swift)
        self.assertIn("!looksUnconfigured(configuredManifestDigest)", self.swift)
        self.assertIn("exit(ExitCode.config.rawValue)", self.swift)

    def test_swift_source_avoids_shell_and_private_helper_surfaces(self):
        banned_fragments = [
            "EventKit",
            "AppleScript",
            "NSAppleScript",
            "osascript",
            "remctl-bridge",
            "remctl-private",
            "/bin/sh",
            "/bin/bash",
            "/bin/zsh",
        ]
        for fragment in banned_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, self.swift)

    def test_swift_source_uses_fixed_python_role_and_minimal_environment(self):
        self.assertIn('if arguments == ["--permission-status"]', self.swift)
        self.assertIn('arguments[0] == "--run-read-broker"', self.swift)
        self.assertIn('arguments[1] == "--socket"', self.swift)
        self.assertIn('"PATH": minimalExecutablePath', self.swift)
        self.assertIn('process.currentDirectoryURL = URL(fileURLWithPath: "/")', self.swift)
        self.assertIn('"HOME": FileManager.default.homeDirectoryForCurrentUser.path', self.swift)
        self.assertIn('"LANG": "en_US.UTF-8"', self.swift)
        self.assertIn('"LC_ALL": "en_US.UTF-8"', self.swift)
        self.assertIn('"-I"', self.swift)
        self.assertIn('"-S"', self.swift)
        self.assertIn('"--manifest"', self.swift)
        self.assertIn("sealed.manifestPath", self.swift)
        self.assertIn('"--manifest-digest"', self.swift)
        self.assertIn("sealed.manifestDigest", self.swift)
        self.assertIn("sealed.brokerEntrypoint", self.swift)
        self.assertIn("socketPath", self.swift)

    def test_swift_source_validates_absolute_paths_and_reports_read_only_probe(self):
        self.assertIn('rawValue.first == "/"', self.swift)
        self.assertIn('scalar.value == 0 || scalar == "\\n" || scalar == "\\r"', self.swift)
        self.assertIn("Bundle.main.bundleIdentifier == expectedBundleIdentifier", self.swift)
        self.assertIn("hostExecutableIsInsideExpectedBundle()", self.swift)
        self.assertIn("validateRuntimeManifest(sealed: sealed)", self.swift)
        self.assertIn("constantTimeEquals(sha256Hex(manifestData), sealed.manifestDigest)", self.swift)
        self.assertIn("constantTimeEquals(protectedPythonDigest, manifest.protectedPython.sha256)", self.swift)
        self.assertIn("constantTimeEquals(brokerDigest, manifest.brokerEntrypoint.sha256)", self.swift)
        self.assertIn("permissionProbeReadBytes = 1", self.swift)
        self.assertIn('url.lastPathComponent.hasPrefix("Data-")', self.swift)
        self.assertIn('url.pathExtension == "sqlite"', self.swift)
        self.assertIn('"fullDiskAccess": probe.fullDiskAccess', self.swift)
        self.assertIn('"databaseProbe": probe.databaseProbe', self.swift)


if __name__ == "__main__":
    unittest.main()
