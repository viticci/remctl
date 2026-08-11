import Foundation

private let helperName = "RemCTL Agent Helper.app"
private let helperBundleIdentifier = "com.viticci.remctl.agent-helper"
private let maximumRequestBytes = 1_048_576

private struct CommandResult: Decodable {
    let exitCode: Int32
    let stdout: String
    let stderr: String
}

private func helperURL() -> URL {
    if let override = ProcessInfo.processInfo.environment["REMCTL_AGENT_HELPER_APP"],
       !override.isEmpty {
        return URL(fileURLWithPath: override).standardizedFileURL
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Applications", isDirectory: true)
        .appendingPathComponent(helperName, isDirectory: true)
}

private func validateHelper(at url: URL) throws {
    let infoURL = url.appendingPathComponent("Contents/Info.plist")
    guard let data = try? Data(contentsOf: infoURL),
          let info = try? PropertyListSerialization.propertyList(from: data, format: nil),
          let dictionary = info as? [String: Any],
          dictionary["CFBundleIdentifier"] as? String == helperBundleIdentifier else {
        throw NSError(
            domain: "RemCTLAgent",
            code: 69,
            userInfo: [NSLocalizedDescriptionKey:
                "Expected \(helperName) with bundle identifier \(helperBundleIdentifier) at \(url.path)."
            ]
        )
    }
}

private func appleScriptLiteral(_ value: String) -> String {
    value.replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
}

private func readProcess(_ process: Process, stdout: Pipe, stderr: Pipe) throws -> (Data, Data) {
    let reads = DispatchGroup()
    let lock = NSLock()
    var stdoutData = Data()
    var stderrData = Data()
    reads.enter()
    DispatchQueue.global(qos: .userInitiated).async {
        let data = stdout.fileHandleForReading.readDataToEndOfFile()
        lock.lock()
        stdoutData = data
        lock.unlock()
        reads.leave()
    }
    reads.enter()
    DispatchQueue.global(qos: .userInitiated).async {
        let data = stderr.fileHandleForReading.readDataToEndOfFile()
        lock.lock()
        stderrData = data
        lock.unlock()
        reads.leave()
    }
    do {
        try process.run()
    } catch {
        stdout.fileHandleForWriting.closeFile()
        stderr.fileHandleForWriting.closeFile()
        reads.wait()
        throw error
    }
    process.waitUntilExit()
    reads.wait()
    lock.lock()
    let capturedStdout = stdoutData
    let capturedStderr = stderrData
    lock.unlock()
    return (capturedStdout, capturedStderr)
}

private func send(arguments: [String], to helper: URL) throws -> CommandResult {
    let requestData = try JSONSerialization.data(withJSONObject: arguments)
    guard requestData.count <= maximumRequestBytes,
          let request = String(data: requestData, encoding: .utf8) else {
        throw NSError(
            domain: "RemCTLAgent",
            code: 64,
            userInfo: [NSLocalizedDescriptionKey: "Request exceeds 1 MiB or is not valid UTF-8."]
        )
    }

    let appPath = appleScriptLiteral(helper.path)
    let source = """
    on run argv
        set requestJSON to item 1 of argv
        using terms from application "\(appPath)"
            tell application "\(appPath)" to run remctl requestJSON
        end using terms from
    end run
    """

    let process = Process()
    let stdoutPipe = Pipe()
    let stderrPipe = Pipe()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    process.arguments = ["-e", source, "--", request]
    process.standardOutput = stdoutPipe
    process.standardError = stderrPipe
    let (stdoutData, stderrData) = try readProcess(
        process,
        stdout: stdoutPipe,
        stderr: stderrPipe
    )
    let diagnostic = String(decoding: stderrData, as: UTF8.self)
    guard process.terminationStatus == 0 else {
        let message: String
        if diagnostic.contains("-1743") || diagnostic.localizedCaseInsensitiveContains("not authorized") {
            message = "macOS blocked Automation access to \(helperName). Allow the current host app in System Settings > Privacy & Security > Automation, then retry."
        } else {
            message = diagnostic.isEmpty ? "Apple Event delivery to \(helperName) failed." : diagnostic
        }
        throw NSError(
            domain: "RemCTLAgent",
            code: Int(process.terminationStatus),
            userInfo: [NSLocalizedDescriptionKey: message.trimmingCharacters(in: .whitespacesAndNewlines)]
        )
    }

    let response = String(decoding: stdoutData, as: UTF8.self)
        .trimmingCharacters(in: .whitespacesAndNewlines)
    guard let data = response.data(using: .utf8) else {
        throw NSError(
            domain: "RemCTLAgent",
            code: 70,
            userInfo: [NSLocalizedDescriptionKey: "The Agent Helper returned an invalid reply."]
        )
    }
    return try JSONDecoder().decode(CommandResult.self, from: data)
}

func main() -> Int32 {
    let helper = helperURL()
    guard FileManager.default.fileExists(atPath: helper.path) else {
        fputs("RemCTL Agent Helper is not installed at \(helper.path).\n", stderr)
        fputs("Run scripts/install-agent-helper.sh from the RemCTL checkout.\n", stderr)
        return 69
    }
    do {
        try validateHelper(at: helper)
        let result = try send(arguments: Array(CommandLine.arguments.dropFirst()), to: helper)
        FileHandle.standardOutput.write(Data(result.stdout.utf8))
        FileHandle.standardError.write(Data(result.stderr.utf8))
        return result.exitCode
    } catch {
        fputs("remctl-agent: \(error.localizedDescription)\n", stderr)
        return 70
    }
}

exit(main())
