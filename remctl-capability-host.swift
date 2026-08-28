import CryptoKit
import Darwin
import Foundation

private let expectedBundleIdentifier = "net.macstories.remctl.capability-host"
private let expectedLaunchAgentLabel = "net.macstories.remctl.read-broker"
private let expectedBundleExecutable = "remctl-capability-host"
private let minimalExecutablePath = "/usr/bin:/bin:/usr/sbin:/sbin"
private let unconfiguredSentinelPrefix = "__REMCTL_"
private let configuredPythonExecutable = "__REMCTL_CAPABILITY_HOST_PYTHON__"
private let configuredBrokerEntrypoint = "__REMCTL_READ_BROKER_ENTRYPOINT__"
private let configuredManifestPath = "__REMCTL_CAPABILITY_HOST_MANIFEST__"
private let configuredManifestDigest = "__REMCTL_CAPABILITY_HOST_MANIFEST_DIGEST__"
private let expectedHostRole = "read-only-capability-host"
private let expectedProtocolVersion = 1
private let expectedSchemaManifestVersion = 1
private let expectedSchemaManifestDigest =
    "835ae40e7e652bf0a2f4b0c4c04133a971edd2a6c13de03856cdce6a31c689d6"
private let remindersStoreRelativePath =
    "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores"
private let maxPermissionProbeEntries = 16
private let permissionProbeReadBytes = 1

private enum ExitCode: Int32 {
    case success = 0
    case usage = 64
    case software = 70
    case config = 78
}

private struct SealedConfiguration {
    let pythonExecutable: String
    let brokerEntrypoint: String
    let manifestPath: String
    let manifestDigest: String
}

private struct ManifestFileRecord {
    let path: String
    let sha256: String
}

private struct RuntimeFileRecord {
    let key: String
    let path: String
    let sha256: String
}

private struct RuntimeManifest {
    let role: String
    let bundleIdentifier: String
    let launchAgentLabel: String
    let cliVersion: String
    let hostVersion: String
    let protocolVersion: Int
    let schemaManifestVersion: Int
    let schemaManifestDigest: String
    let protectedPython: ManifestFileRecord
    let brokerEntrypoint: ManifestFileRecord
    let runtimeFiles: [RuntimeFileRecord]
}

private struct PermissionProbe {
    let directoryPath: String
    let directoryReadable: Bool
    let databaseProbe: String
    let fullDiskAccess: String
}

private enum Command {
    case permissionStatus
    /// Verify sealed configuration and runtime manifest without starting the broker.
    /// Exits 0 on success, non-zero on any identity or manifest failure.
    /// Used by the installer as a post-build hard-failure gate.
    case verify
    case runReadBroker(socketPath: String)
}

private func pathContainsRejectedCharacters(_ value: String) -> Bool {
    value.unicodeScalars.contains { scalar in
        scalar.value == 0 || scalar == "\n" || scalar == "\r"
    }
}

private func validatedAbsolutePath(_ rawValue: String) -> String? {
    guard !rawValue.isEmpty,
          rawValue.first == "/",
          !pathContainsRejectedCharacters(rawValue)
    else { return nil }
    let normalized = URL(fileURLWithPath: rawValue).standardizedFileURL.path
    guard !normalized.isEmpty, normalized.first == "/" else { return nil }
    return normalized
}

private func looksUnconfigured(_ value: String) -> Bool {
    value.hasPrefix(unconfiguredSentinelPrefix) && value.hasSuffix("__")
}

private func isHexDigest(_ value: String) -> Bool {
    value.count == 64 && value.unicodeScalars.allSatisfy { scalar in
        switch scalar.value {
        case 48...57, 97...102:
            return true
        default:
            return false
        }
    }
}

private func isRuntimeFileKey(_ value: String) -> Bool {
    !value.isEmpty && value.unicodeScalars.allSatisfy { scalar in
        switch scalar.value {
        case 45, 46, 95,
             48...57,
             65...90,
             97...122:
            return true
        default:
            return false
        }
    }
}

private func constantTimeEquals(_ lhs: String, _ rhs: String) -> Bool {
    let left = Array(lhs.utf8)
    let right = Array(rhs.utf8)
    guard left.count == right.count else { return false }
    var difference: UInt8 = 0
    for index in left.indices {
        difference |= left[index] ^ right[index]
    }
    return difference == 0
}

private func withFileSystemPath<T>(
    _ path: String,
    _ body: (UnsafePointer<CChar>) -> T?
) -> T? {
    URL(fileURLWithPath: path).withUnsafeFileSystemRepresentation { pointer in
        guard let pointer else { return nil }
        return body(pointer)
    }
}

private func fileMetadata(atPath path: String) -> stat? {
    var metadata = stat()
    guard withFileSystemPath(path, { pointer in
        lstat(pointer, &metadata) == 0 ? true : nil
    }) != nil else { return nil }
    return metadata
}

private func isRegularFileMode(_ mode: mode_t) -> Bool {
    (mode & S_IFMT) == S_IFREG
}

private func isSymlinkMode(_ mode: mode_t) -> Bool {
    (mode & S_IFMT) == S_IFLNK
}

private func secureReadRegularFile(
    atPath path: String,
    requireExecutable: Bool = false
) -> Data? {
    guard let preflight = fileMetadata(atPath: path),
          !isSymlinkMode(preflight.st_mode),
          isRegularFileMode(preflight.st_mode)
    else { return nil }

    guard let descriptor = withFileSystemPath(path, { pointer in
        let opened = open(pointer, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        return opened >= 0 ? opened : nil
    }) else {
        return nil
    }
    guard descriptor >= 0 else { return nil }
    defer { close(descriptor) }

    var opened = stat()
    guard fstat(descriptor, &opened) == 0,
          isRegularFileMode(opened.st_mode),
          preflight.st_dev == opened.st_dev,
          preflight.st_ino == opened.st_ino
    else { return nil }
    if requireExecutable,
       (opened.st_mode & (S_IXUSR | S_IXGRP | S_IXOTH)) == 0 {
        return nil
    }
    let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: false)
    return try? handle.readToEnd()
}

private func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func sha256HexForFile(
    atPath path: String,
    requireExecutable: Bool = false
) -> String? {
    guard let data = secureReadRegularFile(
        atPath: path,
        requireExecutable: requireExecutable
    ) else {
        return nil
    }
    return sha256Hex(data)
}

private func isRegularReadableFile(_ path: String) -> Bool {
    secureReadRegularFile(atPath: path) != nil
}

private func isExecutableRegularFile(_ path: String) -> Bool {
    secureReadRegularFile(atPath: path, requireExecutable: true) != nil
}

private func parseManifestFileRecord(
    _ rawValue: Any,
    label: String
) -> ManifestFileRecord? {
    guard let payload = rawValue as? [String: Any],
          Set(payload.keys) == Set(["path", "sha256"]),
          let rawPath = payload["path"] as? String,
          let path = validatedAbsolutePath(rawPath),
          let digest = payload["sha256"] as? String,
          isHexDigest(digest)
    else { return nil }
    return ManifestFileRecord(path: path, sha256: digest)
}

private func parseRuntimeFileRecord(
    _ rawValue: Any
) -> RuntimeFileRecord? {
    guard let payload = rawValue as? [String: Any],
          Set(payload.keys) == Set(["key", "path", "sha256"]),
          let key = payload["key"] as? String,
          isRuntimeFileKey(key),
          let rawPath = payload["path"] as? String,
          let path = validatedAbsolutePath(rawPath),
          let digest = payload["sha256"] as? String,
          isHexDigest(digest)
    else { return nil }
    return RuntimeFileRecord(key: key, path: path, sha256: digest)
}

private func parseRuntimeManifest(
    data: Data,
    sealed: SealedConfiguration
) -> RuntimeManifest? {
    guard let rawObject = try? JSONSerialization.jsonObject(with: data),
          let payload = rawObject as? [String: Any]
    else { return nil }

    let expectedKeys: Set<String> = [
        "runtimeManifestVersion",
        "role",
        "bundleIdentifier",
        "launchAgentLabel",
        "cliVersion",
        "hostVersion",
        "protocolVersion",
        "schemaManifestVersion",
        "schemaManifestDigest",
        "protectedPython",
        "brokerEntrypoint",
        "runtimeFiles",
    ]
    guard Set(payload.keys) == expectedKeys,
          let runtimeManifestVersion = payload["runtimeManifestVersion"] as? Int,
          runtimeManifestVersion == 1,
          let role = payload["role"] as? String,
          role == expectedHostRole,
          let bundleIdentifier = payload["bundleIdentifier"] as? String,
          bundleIdentifier == expectedBundleIdentifier,
          let launchAgentLabel = payload["launchAgentLabel"] as? String,
          launchAgentLabel == expectedLaunchAgentLabel,
          let cliVersion = payload["cliVersion"] as? String,
          !cliVersion.isEmpty,
          !pathContainsRejectedCharacters(cliVersion),
          let hostVersion = payload["hostVersion"] as? String,
          hostVersion == (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String),
          let protocolVersion = payload["protocolVersion"] as? Int,
          protocolVersion == expectedProtocolVersion,
          let schemaManifestVersion = payload["schemaManifestVersion"] as? Int,
          schemaManifestVersion == expectedSchemaManifestVersion,
          let schemaManifestDigest = payload["schemaManifestDigest"] as? String,
          isHexDigest(schemaManifestDigest),
          let protectedPython = parseManifestFileRecord(payload["protectedPython"] as Any, label: "protectedPython"),
          let brokerEntrypoint = parseManifestFileRecord(payload["brokerEntrypoint"] as Any, label: "brokerEntrypoint"),
          let runtimeFilesRaw = payload["runtimeFiles"] as? [Any],
          !runtimeFilesRaw.isEmpty
    else { return nil }

    var runtimeFiles: [RuntimeFileRecord] = []
    var seenKeys = Set<String>()
    var seenPaths = Set<String>()
    for item in runtimeFilesRaw {
        guard let record = parseRuntimeFileRecord(item) else { return nil }
        guard seenKeys.insert(record.key).inserted,
              seenPaths.insert(record.path).inserted
        else { return nil }
        runtimeFiles.append(record)
    }

    guard protectedPython.path == sealed.pythonExecutable,
          brokerEntrypoint.path == sealed.brokerEntrypoint,
          seenPaths.insert(protectedPython.path).inserted,
          seenPaths.insert(brokerEntrypoint.path).inserted,
          constantTimeEquals(schemaManifestDigest, expectedSchemaManifestDigest)
    else { return nil }

    return RuntimeManifest(
        role: role,
        bundleIdentifier: bundleIdentifier,
        launchAgentLabel: launchAgentLabel,
        cliVersion: cliVersion,
        hostVersion: hostVersion,
        protocolVersion: protocolVersion,
        schemaManifestVersion: schemaManifestVersion,
        schemaManifestDigest: schemaManifestDigest,
        protectedPython: protectedPython,
        brokerEntrypoint: brokerEntrypoint,
        runtimeFiles: runtimeFiles
    )
}

private func validateRuntimeManifest(
    sealed: SealedConfiguration
) -> RuntimeManifest? {
    guard let manifestData = secureReadRegularFile(atPath: sealed.manifestPath),
          constantTimeEquals(sha256Hex(manifestData), sealed.manifestDigest),
          let manifest = parseRuntimeManifest(data: manifestData, sealed: sealed),
          let protectedPythonDigest = sha256HexForFile(
            atPath: manifest.protectedPython.path,
            requireExecutable: true
          ),
          constantTimeEquals(protectedPythonDigest, manifest.protectedPython.sha256),
          let brokerDigest = sha256HexForFile(atPath: manifest.brokerEntrypoint.path),
          constantTimeEquals(brokerDigest, manifest.brokerEntrypoint.sha256)
    else { return nil }
    return manifest
}

private func hostExecutableIsInsideExpectedBundle() -> Bool {
    guard Bundle.main.bundleIdentifier == expectedBundleIdentifier,
          let executableURL = Bundle.main.executableURL?.standardizedFileURL
    else { return false }
    let bundleURL = Bundle.main.bundleURL.standardizedFileURL
    guard executableURL.path == bundleURL
            .appendingPathComponent("Contents/MacOS/\(expectedBundleExecutable)")
            .path,
          executableURL.path.hasPrefix(bundleURL.path + "/Contents/MacOS/"),
          isExecutableRegularFile(executableURL.path)
    else { return false }
    return true
}

private func loadSealedConfiguration() -> SealedConfiguration? {
    guard hostExecutableIsInsideExpectedBundle(),
          !looksUnconfigured(configuredPythonExecutable),
          !looksUnconfigured(configuredBrokerEntrypoint),
          !looksUnconfigured(configuredManifestPath),
          !looksUnconfigured(configuredManifestDigest),
          let pythonExecutable = validatedAbsolutePath(configuredPythonExecutable),
          let brokerEntrypoint = validatedAbsolutePath(configuredBrokerEntrypoint),
          let manifestPath = validatedAbsolutePath(configuredManifestPath),
          isHexDigest(configuredManifestDigest),
          isExecutableRegularFile(pythonExecutable),
          isRegularReadableFile(brokerEntrypoint),
          isRegularReadableFile(manifestPath)
    else { return nil }
    let sealed = SealedConfiguration(
        pythonExecutable: pythonExecutable,
        brokerEntrypoint: brokerEntrypoint,
        manifestPath: manifestPath,
        manifestDigest: configuredManifestDigest
    )
    guard validateRuntimeManifest(sealed: sealed) != nil else {
        return nil
    }
    return sealed
}

private func parseCommandLine() -> Command? {
    let arguments = Array(CommandLine.arguments.dropFirst())
    if arguments == ["--permission-status"] {
        return .permissionStatus
    }
    if arguments == ["--verify"] {
        return .verify
    }
    guard arguments.count == 3,
          arguments[0] == "--run-read-broker",
          arguments[1] == "--socket",
          let socketPath = validatedAbsolutePath(arguments[2])
    else { return nil }
    return .runReadBroker(socketPath: socketPath)
}

/// Verify sealed configuration, bundle identity, and runtime manifest integrity
/// without starting the broker or touching the Reminders database.
/// Designed for installer post-build validation; safe to call in any environment.
private func runVerify() -> Never {
    // Check that all unconfigured sentinels have been substituted.
    guard !looksUnconfigured(configuredPythonExecutable),
          !looksUnconfigured(configuredBrokerEntrypoint),
          !looksUnconfigured(configuredManifestPath),
          !looksUnconfigured(configuredManifestDigest) else {
        fputs("verify: one or more sentinel values are still unconfigured\n", stderr)
        exit(ExitCode.config.rawValue)
    }
    // Check bundle identity.
    guard Bundle.main.bundleIdentifier == expectedBundleIdentifier else {
        fputs("verify: bundle identifier mismatch\n", stderr)
        exit(ExitCode.software.rawValue)
    }
    guard hostExecutableIsInsideExpectedBundle() else {
        fputs("verify: executable is not inside the expected bundle\n", stderr)
        exit(ExitCode.software.rawValue)
    }
    // Validate the sealed configuration (paths + runtime manifest).
    guard loadSealedConfiguration() != nil else {
        fputs("verify: sealed configuration failed to load or validate\n", stderr)
        exit(ExitCode.software.rawValue)
    }
    print("verify: sealed configuration and runtime manifest OK")
    exit(ExitCode.success.rawValue)
}

private func childEnvironment() -> [String: String] {
    [
        "HOME": FileManager.default.homeDirectoryForCurrentUser.path,
        "PATH": minimalExecutablePath,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    ]
}

private func launchReadBroker(
    sealed: SealedConfiguration,
    socketPath: String
) -> Process? {
    guard validateRuntimeManifest(sealed: sealed) != nil else {
        return nil
    }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: sealed.pythonExecutable)
    process.arguments = [
        "-I",
        "-S",
        sealed.brokerEntrypoint,
        "--manifest",
        sealed.manifestPath,
        "--manifest-digest",
        sealed.manifestDigest,
        "--socket",
        socketPath,
    ]
    process.environment = childEnvironment()
    process.currentDirectoryURL = URL(fileURLWithPath: "/")
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
        return process
    } catch {
        return nil
    }
}

private func forwardSignals(to process: Process) -> [DispatchSourceSignal] {
    let signals: [Int32] = [SIGTERM, SIGINT]
    return signals.compactMap { signalNumber in
        signal(signalNumber, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .global())
        source.setEventHandler {
            kill(process.processIdentifier, signalNumber)
        }
        source.resume()
        return source
    }
}

private func childExitCode(for process: Process) -> Int32 {
    process.waitUntilExit()
    if process.terminationReason == .uncaughtSignal {
        return 128 + process.terminationStatus
    }
    return process.terminationStatus
}

private func runReadBroker(socketPath: String) -> Never {
    guard let sealed = loadSealedConfiguration() else {
        exit(ExitCode.config.rawValue)
    }
    guard let child = launchReadBroker(sealed: sealed, socketPath: socketPath) else {
        exit(ExitCode.software.rawValue)
    }
    let forwardedSignals = forwardSignals(to: child)
    let status = childExitCode(for: child)
    forwardedSignals.forEach { $0.cancel() }
    exit(status)
}

private func remindersStoreURL() -> URL {
    FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(remindersStoreRelativePath, isDirectory: true)
}

private func probeDatabaseReadability(at url: URL) -> Bool {
    do {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        _ = try handle.read(upToCount: permissionProbeReadBytes)
        return true
    } catch {
        return false
    }
}

private func permissionProbe() -> PermissionProbe {
    let storeURL = remindersStoreURL()
    let fileManager = FileManager.default
    do {
        let entries = try fileManager.contentsOfDirectory(
            at: storeURL,
            includingPropertiesForKeys: [.isRegularFileKey],
            options: [.skipsHiddenFiles]
        )
        let candidates = entries
            .filter { url in
                url.lastPathComponent.hasPrefix("Data-") && url.pathExtension == "sqlite"
            }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
            .prefix(maxPermissionProbeEntries)
        if candidates.isEmpty {
            return PermissionProbe(
                directoryPath: storeURL.path,
                directoryReadable: true,
                databaseProbe: "notFound",
                fullDiskAccess: "indeterminate"
            )
        }
        for candidate in candidates where probeDatabaseReadability(at: candidate) {
            return PermissionProbe(
                directoryPath: storeURL.path,
                directoryReadable: true,
                databaseProbe: "readable",
                fullDiskAccess: "authorized"
            )
        }
        return PermissionProbe(
            directoryPath: storeURL.path,
            directoryReadable: true,
            databaseProbe: "notReadable",
            fullDiskAccess: "denied"
        )
    } catch {
        return PermissionProbe(
            directoryPath: storeURL.path,
            directoryReadable: false,
            databaseProbe: "unavailable",
            fullDiskAccess: "denied"
        )
    }
}

private func emitPermissionStatus() -> Never {
    let probe = permissionProbe()
    let payload: [String: Any] = [
        "bundleIdentifier": expectedBundleIdentifier,
        "directoryReadable": probe.directoryReadable,
        "fullDiskAccess": probe.fullDiskAccess,
        "launchAgentLabel": expectedLaunchAgentLabel,
        "probeDirectory": probe.directoryPath,
        "databaseProbe": probe.databaseProbe,
        "status": "ok",
    ]
    guard JSONSerialization.isValidJSONObject(payload),
          let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
          let output = String(data: data, encoding: .utf8)
    else {
        exit(ExitCode.software.rawValue)
    }
    print(output)
    exit(ExitCode.success.rawValue)
}

guard let command = parseCommandLine() else {
    exit(ExitCode.usage.rawValue)
}

switch command {
case .permissionStatus:
    emitPermissionStatus()
case .verify:
    runVerify()
case let .runReadBroker(socketPath):
    runReadBroker(socketPath: socketPath)
}
