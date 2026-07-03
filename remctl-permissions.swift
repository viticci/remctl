import AppKit
import Foundation

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
    case verified
    case helperStoreReadable
    case needsAccess
}

struct Options {
    var title = "RemCTL Permissions"
    var subtitle = "Grant Full Disk Access to the processes RemCTL uses."
    var autoOpenSettings = true
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
        default:
            index += 1
        }
    }
    if options.targets.isEmpty {
        options.targets.append(PermissionTarget(
            title: "Current Python interpreter",
            path: "/usr/bin/python3",
            subtitle: "Fallback target for direct CLI reads."
        ))
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

func remindersStoreReadable() -> Bool {
    let storesURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Group Containers/group.com.apple.reminders/Container_v1/Stores")
    guard let contents = try? FileManager.default.contentsOfDirectory(
        at: storesURL,
        includingPropertiesForKeys: nil,
        options: [.skipsHiddenFiles]
    ) else {
        return false
    }
    return contents.contains { url in
        url.lastPathComponent.hasPrefix("Data-") &&
            url.pathExtension == "sqlite" &&
            FileManager.default.isReadableFile(atPath: url.path)
    }
}

func isPythonExecutable(_ path: String) -> Bool {
    let name = URL(fileURLWithPath: path).lastPathComponent.lowercased()
    return name.hasPrefix("python") && FileManager.default.isExecutableFile(atPath: path)
}

func pythonFrameworkRoot(for path: String) -> URL? {
    let url = URL(fileURLWithPath: path).standardizedFileURL
    let components = url.pathComponents
    guard let frameworkIndex = components.firstIndex(of: "Python.framework"),
          frameworkIndex + 2 < components.count,
          components[frameworkIndex + 1] == "Versions" else {
        return nil
    }
    var root = URL(fileURLWithPath: "/")
    for component in components[1...(frameworkIndex + 2)] {
        root.appendPathComponent(component)
    }
    return root
}

func pythonIcon(for path: String) -> NSImage? {
    let candidates: [URL]
    if let frameworkRoot = pythonFrameworkRoot(for: path) {
        candidates = [
            frameworkRoot.appendingPathComponent("Resources/Python.app/Contents/Resources/PythonInterpreter.icns"),
            frameworkRoot.appendingPathComponent("Resources/Python.app/Contents/Resources/PythonApplet.icns"),
        ]
    } else {
        candidates = [
            URL(fileURLWithPath: "/Library/Frameworks/Python.framework/Versions/Current/Resources/Python.app/Contents/Resources/PythonInterpreter.icns"),
            URL(fileURLWithPath: "/Library/Frameworks/Python.framework/Versions/Current/Resources/Python.app/Contents/Resources/PythonApplet.icns"),
        ]
    }
    for candidate in candidates where FileManager.default.fileExists(atPath: candidate.path) {
        if let image = NSImage(contentsOf: candidate) {
            return image
        }
    }
    return nil
}

func targetIcon(for target: PermissionTarget) -> NSImage {
    if isPythonExecutable(target.path), let image = pythonIcon(for: target.path) {
        return image
    }
    return NSWorkspace.shared.icon(forFile: target.path)
}

func pythonTargetCanReadRemindersStore(_ path: String) -> Bool {
    let script = """
import glob
import os
import sys

base = os.path.expanduser("~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores")
try:
    paths = glob.glob(os.path.join(base, "Data-*.sqlite"))
    ok = any(os.path.isfile(path) and os.access(path, os.R_OK) for path in paths)
except Exception:
    ok = False
sys.exit(0 if ok else 2)
"""
    let process = Process()
    process.executableURL = URL(fileURLWithPath: path)
    process.arguments = ["-c", script]
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = Pipe()
    process.standardError = Pipe()
    do {
        try process.run()
        process.waitUntilExit()
        return process.terminationStatus == 0
    } catch {
        return false
    }
}

func verifyPermissionTarget(_ target: PermissionTarget) -> PermissionStatus {
    if isPythonExecutable(target.path) {
        return pythonTargetCanReadRemindersStore(target.path) ? .verified : .needsAccess
    }
    return remindersStoreReadable() ? .helperStoreReadable : .needsAccess
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
        case .verified:
            statusField.stringValue = "✓ Access verified"
            statusField.textColor = .systemGreen
        case .helperStoreReadable:
            statusField.stringValue = "Store readable (helper check — cannot verify \(target.title) directly)"
            statusField.textColor = .systemGreen
        case .needsAccess:
            statusField.stringValue = "Needs access"
            statusField.textColor = .systemOrange
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let options: Options
    private var window: NSWindow?
    private var targetRows: [TargetRowView] = []
    private var refreshTimer: Timer?
    private var refreshInProgress = false
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
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.refreshTargetStatuses()
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTimer?.invalidate()
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

        let checkButton = ActionButton(title: "Check Access") { _ in
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
        if refreshInProgress {
            return
        }
        refreshInProgress = true
        targetRows.forEach { $0.updateStatus(.checking) }
        let targets = options.targets
        DispatchQueue.global(qos: .utility).async {
            let statuses = targets.map { verifyPermissionTarget($0) }
            DispatchQueue.main.async {
                for (row, status) in zip(self.targetRows, statuses) {
                    row.updateStatus(status)
                }
                self.refreshInProgress = false
                if logResult {
                    let accessible = statuses.filter {
                        switch $0 {
                        case .verified, .helperStoreReadable: return true
                        default: return false
                        }
                    }.count
                    self.log("Accessible \(accessible) of \(statuses.count) Full Disk Access targets.")
                }
            }
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

let app = NSApplication.shared
let delegate = AppDelegate(options: parseOptions())
app.delegate = delegate
app.run()
