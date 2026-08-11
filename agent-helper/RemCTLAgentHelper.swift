import AppKit
import Foundation

private let remctlEventClass: AEEventClass = 0x5243544C // 'RCTL'
private let executeEventID: AEEventID = 0x45584543 // 'EXEC'
private let maximumRequestBytes = 1_048_576
private let maximumArguments = 256
private let maximumResponseBytes = 8 * 1_048_576

private struct CommandResult: Codable {
    let exitCode: Int32
    let stdout: String
    let stderr: String
}

private enum HelperError: LocalizedError {
    case missingRuntime
    case invalidRequest(String)
    case responseTooLarge

    var errorDescription: String? {
        switch self {
        case .missingRuntime:
            return "The bundled RemCTL runtime is missing. Reinstall the Agent Helper."
        case .invalidRequest(let detail):
            return "Invalid request: \(detail)"
        case .responseTooLarge:
            return "RemCTL output exceeds the Agent Helper's 8 MiB response limit."
        }
    }
}

private func validatedArguments(from json: String) throws -> [String] {
    guard let data = json.data(using: .utf8), data.count <= maximumRequestBytes else {
        throw HelperError.invalidRequest("payload exceeds 1 MiB")
    }
    let value = try JSONSerialization.jsonObject(with: data)
    guard let arguments = value as? [String], arguments.count <= maximumArguments else {
        throw HelperError.invalidRequest("expected an array of at most 256 strings")
    }
    for argument in arguments {
        if argument.utf8.count > 65_536 || argument.contains("\0") {
            throw HelperError.invalidRequest("an argument contains NUL or exceeds 64 KiB")
        }
    }
    return arguments
}

private func runtimeDirectory() throws -> URL {
    guard let resources = Bundle.main.resourceURL else {
        throw HelperError.missingRuntime
    }
    let runtime = resources.appendingPathComponent("Runtime", isDirectory: true)
    let remctl = runtime.appendingPathComponent("remctl")
    guard FileManager.default.isExecutableFile(atPath: remctl.path) else {
        throw HelperError.missingRuntime
    }
    return runtime
}

private func sanitizedEnvironment(runtime: URL) -> [String: String] {
    var environment = ProcessInfo.processInfo.environment
    for key in environment.keys where key.hasPrefix("DYLD_") {
        environment.removeValue(forKey: key)
    }
    for key in [
        "PYTHONHOME", "PYTHONPATH", "REMCTL_STORE_DIR", "REMCTL_BRIDGE_PATH",
        "REMCTL_PRIVATE_PATH", "REMCTL_PERMISSIONS_PATH", "REMCTL_PATH",
    ] {
        environment.removeValue(forKey: key)
    }
    environment["REMCTL_PATH"] = runtime.appendingPathComponent("remctl").path
    environment["REMCTL_BRIDGE_PATH"] = runtime.appendingPathComponent("remctl-bridge").path
    environment["REMCTL_PRIVATE_PATH"] = runtime.appendingPathComponent("remctl-private").path
    environment["REMCTL_PERMISSIONS_PATH"] = runtime.appendingPathComponent("remctl-permissions").path
    return environment
}

private func runRemCTL(arguments: [String]) throws -> CommandResult {
    let runtime = try runtimeDirectory()
    let process = Process()
    let stdoutPipe = Pipe()
    let stderrPipe = Pipe()
    process.executableURL = runtime.appendingPathComponent("remctl")
    process.arguments = arguments
    process.environment = sanitizedEnvironment(runtime: runtime)
    process.standardOutput = stdoutPipe
    process.standardError = stderrPipe

    let reads = DispatchGroup()
    let lock = NSLock()
    var stdoutData = Data()
    var stderrData = Data()

    reads.enter()
    DispatchQueue.global(qos: .userInitiated).async {
        let data = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
        lock.lock()
        stdoutData = data
        lock.unlock()
        reads.leave()
    }
    reads.enter()
    DispatchQueue.global(qos: .userInitiated).async {
        let data = stderrPipe.fileHandleForReading.readDataToEndOfFile()
        lock.lock()
        stderrData = data
        lock.unlock()
        reads.leave()
    }

    do {
        try process.run()
    } catch {
        stdoutPipe.fileHandleForWriting.closeFile()
        stderrPipe.fileHandleForWriting.closeFile()
        reads.wait()
        throw error
    }
    process.waitUntilExit()
    reads.wait()

    lock.lock()
    let capturedStdout = stdoutData
    let capturedStderr = stderrData
    lock.unlock()
    guard capturedStdout.count + capturedStderr.count <= maximumResponseBytes else {
        throw HelperError.responseTooLarge
    }
    return CommandResult(
        exitCode: process.terminationStatus,
        stdout: String(decoding: capturedStdout, as: UTF8.self),
        stderr: String(decoding: capturedStderr, as: UTF8.self)
    )
}

private func encodedResult(_ result: CommandResult) -> String {
    guard let data = try? JSONEncoder().encode(result) else {
        return #"{"exitCode":70,"stdout":"","stderr":"Unable to encode Agent Helper response.\n"}"#
    }
    return String(decoding: data, as: UTF8.self)
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(
            self,
            andSelector: #selector(handleExecuteEvent(_:withReplyEvent:)),
            forEventClass: remctlEventClass,
            andEventID: executeEventID
        )
    }

    func applicationWillTerminate(_ notification: Notification) {
        NSAppleEventManager.shared().removeEventHandler(
            forEventClass: remctlEventClass,
            andEventID: executeEventID
        )
    }

    @objc(handleExecuteEvent:withReplyEvent:)
    func handleExecuteEvent(
        _ event: NSAppleEventDescriptor,
        withReplyEvent reply: NSAppleEventDescriptor
    ) {
        let result: CommandResult
        do {
            guard let payload = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue else {
                throw HelperError.invalidRequest("missing JSON argument array")
            }
            result = try runRemCTL(arguments: validatedArguments(from: payload))
        } catch {
            result = CommandResult(
                exitCode: 70,
                stdout: "",
                stderr: "RemCTL Agent Helper: \(error.localizedDescription)\n"
            )
        }
        reply.setParam(
            NSAppleEventDescriptor(string: encodedResult(result)),
            forKeyword: keyDirectObject
        )
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
