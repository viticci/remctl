import AppKit
import CryptoKit
import Darwin
import Foundation
import Security

struct PermissionTarget {
    let title: String
    let path: String
    let subtitle: String
}

struct AfterCommand {
    let title: String
    let command: String
}

enum PermissionStatus {
    case checking
    case verifyAfterRestart
}

struct Options {
    var title = "RemCTL Permissions"
    var subtitle = "Grant Full Disk Access only to the signed RemCTL Capability Host."
    var autoOpenSettings = true
    var validateOnly = false
    var targets: [PermissionTarget] = []
    var afterCommands: [AfterCommand] = []
}

let fullDiskAccessURLs = [
    "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles",
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
]

func parseOptions() -> Options {
    var options = Options()
    let args = Array(CommandLine.arguments.dropFirst())
    var index = 0
    while index < args.count {
        switch args[index] {
        case "--title" where index + 1 < args.count:
            options.title = args[index + 1]
            index += 2
        case "--subtitle" where index + 1 < args.count:
            options.subtitle = args[index + 1]
            index += 2
        case "--target" where index + 3 < args.count:
            options.targets.append(PermissionTarget(title: args[index + 1], path: args[index + 2], subtitle: args[index + 3]))
            index += 4
        case "--after" where index + 2 < args.count:
            options.afterCommands.append(AfterCommand(title: args[index + 1], command: args[index + 2]))
            index += 3
        case "--no-open":
            options.autoOpenSettings = false
            index += 1
        case "--validate-only":
            options.validateOnly = true
            index += 1
        default:
            index += 1
        }
    }
    return options
}

func copyPath(_ path: String) {
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    pasteboard.setString(path, forType: .string)
    pasteboard.setString(URL(fileURLWithPath: path).absoluteString, forType: .fileURL)
}

func openFullDiskAccessSettings() {
    for rawURL in fullDiskAccessURLs {
        guard let url = URL(string: rawURL) else { continue }
        if NSWorkspace.shared.open(url) {
            return
        }
    }
}

func revealInFinder(_ path: String) {
    NSWorkspace.shared.selectFile(path, inFileViewerRootedAtPath: "")
}

func siblingResourceURL(named filename: String) -> URL? {
    guard let rawExecutable = CommandLine.arguments.first, !rawExecutable.isEmpty else {
        return nil
    }
    let executableURL: URL
    if rawExecutable.hasPrefix("/") {
        executableURL = URL(fileURLWithPath: rawExecutable)
    } else {
        executableURL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent(rawExecutable)
    }
    let candidate = executableURL.deletingLastPathComponent().appendingPathComponent(filename)
    return FileManager.default.fileExists(atPath: candidate.path) ? candidate : nil
}

func applyApplicationIcon() {
    guard let iconURL = siblingResourceURL(named: "remctl-permissions-icon.png"),
          let image = NSImage(contentsOf: iconURL) else {
        return
    }
    NSApp.applicationIconImage = image
}

func applicationIconImage() -> NSImage? {
    if let image = NSApp.applicationIconImage {
        return image
    }
    guard let iconURL = siblingResourceURL(named: "remctl-permissions-icon.png") else {
        return nil
    }
    return NSImage(contentsOf: iconURL)
}

func targetIcon(for target: PermissionTarget) -> NSImage {
    return NSWorkspace.shared.icon(forFile: target.path)
}

func codesignResult(arguments: [String]) -> (status: Int32, output: String)? {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
    process.arguments = arguments
    process.standardInput = FileHandle.nullDevice
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = pipe
    do {
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    } catch {
        return nil
    }
}

func canonicalCapabilityHostURL(_ path: String) -> URL? {
    guard path.hasPrefix("/") else { return nil }
    let standardized = URL(fileURLWithPath: path).standardizedFileURL
    guard standardized.path == path,
          standardized.lastPathComponent == "RemCTL Capability Host.app",
          standardized.resolvingSymlinksInPath().path == path else {
        return nil
    }
    return standardized
}

func installedCapabilityHostPath() -> String? {
    guard let markerURL = siblingResourceURL(named: ".remctl-capability-host-app") else {
        return nil
    }
    var metadata = stat()
    guard lstat(markerURL.path, &metadata) == 0,
          (metadata.st_mode & S_IFMT) == S_IFREG,
          metadata.st_uid == getuid(),
          (metadata.st_mode & 0o022) == 0,
          metadata.st_size > 0,
          metadata.st_size <= 4096,
          let data = try? Data(contentsOf: markerURL),
          let text = String(data: data, encoding: .utf8) else {
        return nil
    }
    let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
    guard lines.count == 2,
          lines[1].isEmpty,
          !lines[0].isEmpty else {
        return nil
    }
    let path = String(lines[0])
    return canonicalCapabilityHostURL(path) == nil ? nil : path
}

func preservedSigningIdentity() -> Data? {
    guard let markerURL = siblingResourceURL(named: ".remctl-capability-host-signing-identity") else {
        return nil
    }
    var metadata = stat()
    guard lstat(markerURL.path, &metadata) == 0,
          (metadata.st_mode & S_IFMT) == S_IFREG,
          metadata.st_uid == getuid(),
          (metadata.st_mode & 0o022) == 0,
          metadata.st_size == 41,
          let data = try? Data(contentsOf: markerURL),
          let text = String(data: data, encoding: .utf8),
          text.last == "\n"
    else { return nil }
    let identity = String(text.dropLast())
    guard identity.count == 40,
          identity.allSatisfy({ $0.isHexDigit }),
          let decoded = Data(hexadecimal: identity)
    else { return nil }
    return decoded
}

extension Data {
    init?(hexadecimal: String) {
        guard hexadecimal.count.isMultiple(of: 2) else { return nil }
        var result = Data(capacity: hexadecimal.count / 2)
        var index = hexadecimal.startIndex
        while index < hexadecimal.endIndex {
            let next = hexadecimal.index(index, offsetBy: 2)
            guard let byte = UInt8(hexadecimal[index..<next], radix: 16) else { return nil }
            result.append(byte)
            index = next
        }
        self = result
    }
}

/// Accept only an identity that a Full Disk Access grant keeps following across
/// updates: the installer's preserved certificate, either under an Apple team
/// or as a local certificate that the designated requirement pins exactly.
/// Ad-hoc and unsigned code has no certificate and is refused.
func signingIdentityError(for target: URL, expectedIdentifier: String) -> String? {
    guard let expectedCertificateHash = preservedSigningIdentity() else {
        return "the preserved signing identity is missing or unsafe"
    }
    var staticCode: SecStaticCode?
    guard SecStaticCodeCreateWithPath(target as CFURL, [], &staticCode) == errSecSuccess,
          let staticCode else {
        return "the target app signature cannot be inspected"
    }
    var information: CFDictionary?
    let informationFlags = SecCSFlags(
        rawValue: kSecCSSigningInformation | kSecCSRequirementInformation
    )
    guard SecCodeCopySigningInformation(
        staticCode,
        informationFlags,
        &information
    ) == errSecSuccess,
          let values = information as NSDictionary?,
          let identifier = values[kSecCodeInfoIdentifier] as? String,
          identifier == expectedIdentifier,
          let certificates = values[kSecCodeInfoCertificates] as? [SecCertificate],
          let leaf = certificates.first
    else { return "the target app has no stable signing identity" }
    var designatedRequirement: SecRequirement?
    guard SecCodeCopyDesignatedRequirement(staticCode, [], &designatedRequirement) == errSecSuccess,
          let designatedRequirement,
          SecStaticCodeCheckValidity(staticCode, [], designatedRequirement) == errSecSuccess
    else { return "the target app does not satisfy its designated requirement" }
    let leafData = SecCertificateCopyData(leaf) as Data
    let leafHash = Data(Insecure.SHA1.hash(data: leafData))
    guard leafHash == expectedCertificateHash else {
        return "the target app does not match the preserved signing identity"
    }
    let teamIdentifier = values[kSecCodeInfoTeamIdentifier] as? String ?? ""
    if teamIdentifier.isEmpty || teamIdentifier == "not set" {
        let leafHex = leafHash.map { String(format: "%02x", $0) }.joined()
        let pinned = "identifier \"\(expectedIdentifier)\" and certificate leaf = H\"\(leafHex)\""
        var requirementText: CFString?
        guard SecRequirementCopyString(designatedRequirement, [], &requirementText) == errSecSuccess,
              let requirementText,
              (requirementText as String).lowercased() == pinned.lowercased()
        else { return "the target app has no stable signing identity" }
    }
    return nil
}

func capabilityHostTargetValidationError(_ target: PermissionTarget) -> String? {
    let expectedIdentifier = "net.macstories.remctl.capability-host"
    guard let installedPath = installedCapabilityHostPath() else {
        return "the installed host marker is missing or unsafe"
    }
    guard installedPath == target.path else {
        return "the target does not match the installed host marker"
    }
    guard let standardized = canonicalCapabilityHostURL(target.path) else {
        return "the target path is not canonical"
    }
    var isDirectory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: target.path, isDirectory: &isDirectory),
          isDirectory.boolValue else {
        return "the target app does not exist"
    }
    let infoURL = standardized.appendingPathComponent("Contents/Info.plist")
    guard let infoData = try? Data(contentsOf: infoURL),
          let info = try? PropertyListSerialization.propertyList(from: infoData, format: nil) as? [String: Any],
          info["CFBundleIdentifier"] as? String == expectedIdentifier,
          let executableName = info["CFBundleExecutable"] as? String,
          !executableName.isEmpty else {
        return "the target app has invalid bundle metadata"
    }
    let executableURL = standardized.appendingPathComponent("Contents/MacOS").appendingPathComponent(executableName)
    guard FileManager.default.isExecutableFile(atPath: executableURL.path),
          executableURL.resolvingSymlinksInPath().path == executableURL.path,
          let verification = codesignResult(arguments: ["--verify", "--deep", "--strict", target.path]),
          verification.status == 0,
          let description = codesignResult(arguments: ["-dvvv", "-r-", target.path]),
          description.status == 0 else {
        return "the target app signature is invalid"
    }
    let descriptionLines = description.output.components(separatedBy: .newlines)
    guard descriptionLines.contains("Identifier=\(expectedIdentifier)") &&
          descriptionLines.contains(where: { $0.contains("designated => ") }) else {
        return "the target app signature identity is invalid"
    }
    if let error = signingIdentityError(for: standardized, expectedIdentifier: expectedIdentifier) {
        return error
    }
    return nil
}

final class ActionButton: NSButton {
    private let handler: (ActionButton) -> Void

    init(title: String, handler: @escaping (ActionButton) -> Void) {
        self.handler = handler
        super.init(frame: .zero)
        self.title = title
        target = self
        action = #selector(performAction)
        bezelStyle = .rounded
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    @objc private func performAction(_ sender: Any?) {
        handler(self)
    }
}

func label(_ text: String, font: NSFont, color: NSColor = .labelColor, lines: Int = 0) -> NSTextField {
    let field = NSTextField(labelWithString: text)
    field.font = font
    field.textColor = color
    field.lineBreakMode = .byWordWrapping
    field.maximumNumberOfLines = lines
    field.translatesAutoresizingMaskIntoConstraints = false
    return field
}

final class TargetRowView: NSView, NSDraggingSource {
    private let target: PermissionTarget
    private let statusField = NSTextField(labelWithString: "Checking...")

    init(target: PermissionTarget, onLog: @escaping (String) -> Void) {
        self.target = target
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        wantsLayer = true
        layer?.cornerRadius = 10
        layer?.borderWidth = 1
        layer?.borderColor = NSColor.separatorColor.cgColor
        layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor

        let icon = NSImageView(image: targetIcon(for: target))
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.imageScaling = .scaleProportionallyUpOrDown

        let titleField = label(target.title, font: .boldSystemFont(ofSize: 14), lines: 1)
        let subtitleField = label(target.subtitle, font: .systemFont(ofSize: 12), color: .secondaryLabelColor, lines: 2)
        let pathField = label(target.path, font: .monospacedSystemFont(ofSize: 11, weight: .regular), color: .tertiaryLabelColor, lines: 1)
        pathField.lineBreakMode = .byTruncatingMiddle
        subtitleField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        pathField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        statusField.font = .systemFont(ofSize: 12, weight: .semibold)
        statusField.textColor = .secondaryLabelColor
        statusField.lineBreakMode = .byTruncatingTail
        statusField.maximumNumberOfLines = 1
        statusField.translatesAutoresizingMaskIntoConstraints = false

        let textStack = NSStackView(views: [titleField, subtitleField, pathField])
        textStack.orientation = .vertical
        textStack.spacing = 3
        textStack.alignment = .leading
        textStack.translatesAutoresizingMaskIntoConstraints = false
        textStack.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let copyButton = ActionButton(title: "Copy Path") { _ in
            copyPath(target.path)
            onLog("Copied: \(target.path)")
        }

        let revealButton = ActionButton(title: "Reveal") { _ in
            revealInFinder(target.path)
            onLog("Revealed in Finder: \(target.path)")
        }

        let buttonStack = NSStackView(views: [copyButton, revealButton])
        buttonStack.orientation = .horizontal
        buttonStack.spacing = 8
        buttonStack.translatesAutoresizingMaskIntoConstraints = false

        let trailingStack = NSStackView(views: [statusField, buttonStack])
        trailingStack.orientation = .vertical
        trailingStack.spacing = 8
        trailingStack.alignment = .trailing
        trailingStack.translatesAutoresizingMaskIntoConstraints = false

        addSubview(icon)
        addSubview(textStack)
        addSubview(trailingStack)

        NSLayoutConstraint.activate([
            heightAnchor.constraint(greaterThanOrEqualToConstant: 92),
            icon.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            icon.centerYAnchor.constraint(equalTo: centerYAnchor),
            icon.widthAnchor.constraint(equalToConstant: 38),
            icon.heightAnchor.constraint(equalToConstant: 38),

            textStack.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 12),
            textStack.centerYAnchor.constraint(equalTo: centerYAnchor),
            textStack.widthAnchor.constraint(lessThanOrEqualToConstant: 300),
            textStack.trailingAnchor.constraint(lessThanOrEqualTo: trailingStack.leadingAnchor, constant: -12),

            trailingStack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            trailingStack.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])

        toolTip = "Drag this row into Full Disk Access, or copy the path and use Command-Shift-G."
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func mouseDown(with event: NSEvent) {
        copyPath(target.path)
        let url = NSURL(fileURLWithPath: target.path)
        let item = NSDraggingItem(pasteboardWriter: url)
        let dragImage = targetIcon(for: target)
        dragImage.size = NSSize(width: 64, height: 64)
        item.setDraggingFrame(NSRect(x: 0, y: 0, width: 64, height: 64), contents: dragImage)
        beginDraggingSession(with: [item], event: event, source: self)
    }

    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation {
        return .copy
    }

    func updateStatus(_ status: PermissionStatus) {
        switch status {
        case .checking:
            statusField.stringValue = "Checking..."
            statusField.textColor = .secondaryLabelColor
        case .verifyAfterRestart:
            statusField.stringValue = "Verify after restarting the signed host"
            statusField.textColor = .secondaryLabelColor
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let options: Options
    private var window: NSWindow?
    private var targetRows: [TargetRowView] = []
    private let outputView = NSTextView()

    init(options: Options) {
        self.options = options
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        applyApplicationIcon()
        buildWindow()
        if options.autoOpenSettings {
            openFullDiskAccessSettings()
            log("Opened System Settings > Privacy & Security > Full Disk Access.")
        }
        if let first = options.targets.first {
            copyPath(first.path)
            log("Copied first target. In the file picker press Command-Shift-G, paste, press Return, then click Open.")
        }
        refreshTargetStatuses()
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildWindow() {
        let content = NSView()
        content.translatesAutoresizingMaskIntoConstraints = false

        let headerIcon = NSImageView(image: applicationIconImage() ?? NSImage(size: NSSize(width: 72, height: 72)))
        headerIcon.translatesAutoresizingMaskIntoConstraints = false
        headerIcon.imageScaling = .scaleProportionallyUpOrDown

        let titleField = label(options.title, font: .boldSystemFont(ofSize: 24), lines: 1)
        titleField.alignment = .center
        let subtitleField = label(options.subtitle, font: .systemFont(ofSize: 14), color: .secondaryLabelColor, lines: 2)
        subtitleField.alignment = .center
        subtitleField.preferredMaxLayoutWidth = 500
        subtitleField.widthAnchor.constraint(lessThanOrEqualToConstant: 500).isActive = true

        let openButton = ActionButton(title: "Open Full Disk Access") { _ in
            openFullDiskAccessSettings()
            self.log("Opened Full Disk Access settings.")
        }
        openButton.keyEquivalent = "\r"

        let checkButton = ActionButton(title: "How to Verify") { _ in
            self.refreshTargetStatuses(logResult: true)
        }

        let quitButton = ActionButton(title: "Done") { _ in
            NSApp.terminate(nil)
        }

        let topButtons = NSStackView(views: [openButton, checkButton, quitButton])
        topButtons.orientation = .horizontal
        topButtons.spacing = 8
        topButtons.alignment = .centerY
        topButtons.translatesAutoresizingMaskIntoConstraints = false

        let buttonRow = NSView()
        buttonRow.translatesAutoresizingMaskIntoConstraints = false
        buttonRow.addSubview(topButtons)
        NSLayoutConstraint.activate([
            topButtons.centerXAnchor.constraint(equalTo: buttonRow.centerXAnchor),
            topButtons.topAnchor.constraint(equalTo: buttonRow.topAnchor),
            topButtons.bottomAnchor.constraint(equalTo: buttonRow.bottomAnchor),
        ])

        let header = NSStackView(views: [headerIcon, titleField, subtitleField, buttonRow])
        header.orientation = .vertical
        header.spacing = 8
        header.alignment = .centerX
        header.translatesAutoresizingMaskIntoConstraints = false

        targetRows = options.targets.map { target in
            TargetRowView(target: target) { [weak self] message in
                self?.log(message)
            }
        }
        let targetsStack = NSStackView(views: targetRows)
        targetsStack.orientation = .vertical
        targetsStack.spacing = 10
        targetsStack.alignment = .width

        outputView.isEditable = false
        outputView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        outputView.textColor = .secondaryLabelColor
        outputView.backgroundColor = .textBackgroundColor
        let outputScroll = NSScrollView()
        outputScroll.hasVerticalScroller = true
        outputScroll.documentView = outputView
        outputScroll.translatesAutoresizingMaskIntoConstraints = false
        outputScroll.heightAnchor.constraint(equalToConstant: 104).isActive = true

        let commandButtons = options.afterCommands.map { command in
            let button = ActionButton(title: command.title) { [weak self] button in
                self?.runCommand(command, sender: button)
            }
            button.toolTip = command.command
            return button
        }
        let commandsStack = NSStackView(views: commandButtons)
        commandsStack.orientation = .horizontal
        commandsStack.spacing = 8
        commandsStack.alignment = .leading

        let headerContainer = NSView()
        headerContainer.translatesAutoresizingMaskIntoConstraints = false
        headerContainer.addSubview(header)

        let mainStack = NSStackView(views: [headerContainer, targetsStack, commandsStack, outputScroll])
        mainStack.orientation = .vertical
        mainStack.spacing = 16
        mainStack.alignment = .width
        mainStack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(mainStack)

        NSLayoutConstraint.activate([
            mainStack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 22),
            mainStack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -22),
            mainStack.topAnchor.constraint(equalTo: content.topAnchor, constant: 22),
            mainStack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -22),
            header.centerXAnchor.constraint(equalTo: headerContainer.centerXAnchor),
            header.topAnchor.constraint(equalTo: headerContainer.topAnchor),
            header.bottomAnchor.constraint(equalTo: headerContainer.bottomAnchor),
            header.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
            headerIcon.widthAnchor.constraint(equalToConstant: 72),
            headerIcon.heightAnchor.constraint(equalToConstant: 72),
            buttonRow.widthAnchor.constraint(equalTo: header.widthAnchor),
        ])

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 600, height: 560),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "RemCTL Permissions"
        window.contentView = content
        window.center()
        window.makeKeyAndOrderFront(nil)
        self.window = window
    }

    private func log(_ text: String) {
        let existing = outputView.string
        outputView.string = existing.isEmpty ? text : "\(existing)\n\(text)"
        outputView.scrollToEndOfDocument(nil)
    }

    private func refreshTargetStatuses(logResult: Bool = false) {
        targetRows.forEach { $0.updateStatus(.checking) }
        targetRows.forEach { $0.updateStatus(.verifyAfterRestart) }
        if logResult {
            self.log("After changing Full Disk Access, click Restart Host + Run Doctor to verify the signed host.")
        }
    }

    private func commandEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        let homeBin = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("bin").path
        let defaultPath = "\(homeBin):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        if let inheritedPath = environment["PATH"], !inheritedPath.isEmpty {
            environment["PATH"] = "\(defaultPath):\(inheritedPath)"
        } else {
            environment["PATH"] = defaultPath
        }
        return environment
    }

    private func runCommand(_ command: AfterCommand, sender: NSButton? = nil) {
        let originalTitle = sender?.title
        sender?.isEnabled = false
        if let originalTitle {
            sender?.title = "\(originalTitle)..."
        }
        log("Running \(command.title): \(command.command)")
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/zsh")
            process.arguments = ["-lc", command.command]
            process.environment = self.commandEnvironment()
            let pipe = Pipe()
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = pipe
            process.standardError = pipe
            do {
                try process.run()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let output = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                DispatchQueue.main.async {
                    if !output.isEmpty {
                        self.log(output)
                    }
                    self.log(process.terminationStatus == 0 ? "Command completed." : "Command failed with exit \(process.terminationStatus).")
                    sender?.isEnabled = true
                    if let originalTitle {
                        sender?.title = originalTitle
                    }
                    self.refreshTargetStatuses()
                }
            } catch {
                DispatchQueue.main.async {
                    self.log("Could not run command: \(error.localizedDescription)")
                    sender?.isEnabled = true
                    if let originalTitle {
                        sender?.title = originalTitle
                    }
                    self.refreshTargetStatuses()
                }
            }
        }
    }
}

let options = parseOptions()
guard options.targets.count == 1,
      let target = options.targets.first else {
    let message = "Error: remctl-permissions requires exactly one canonical, validly signed RemCTL Capability Host.app target. Run `remctl permissions full-disk-access` instead.\n"
    FileHandle.standardError.write(Data(message.utf8))
    exit(64)
}
if let validationError = capabilityHostTargetValidationError(target) {
    // Name only the installed host, never the refused target, which may be any app.
    let hostApp = installedCapabilityHostPath() ?? "RemCTL Capability Host.app"
    let message = """
    Error: remctl-permissions refused the RemCTL Capability Host.app target because \(validationError).
    To grant access by hand, open System Settings > Privacy & Security > Full Disk Access, \
    click +, choose \(hostApp), and turn it on. Then restart the host:
      launchctl kickstart -k gui/\(getuid())/net.macstories.remctl.capability-host
      remctl doctor

    """
    FileHandle.standardError.write(Data(message.utf8))
    exit(64)
}
if options.validateOnly {
    print("valid")
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate(options: options)
app.delegate = delegate
app.run()
