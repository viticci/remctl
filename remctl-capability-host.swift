import AppKit
import CoreServices
import Darwin
import EventKit
import Foundation
import MachO
import Security

private let archiveDescriptor: Int32 = 198
private let archiveFDEnvironment = "REMCTL_CAPABILITY_ARCHIVE_FD"
private let nativeDescriptor: Int32 = 199
private let nativeFDEnvironment = "REMCTL_CAPABILITY_NATIVE_FD"
private let nativeProtocolVersion = 1
private let nativeRequestLimit = 4 * 1024
private let nativeResponseLimit = 64 * 1024
private let nativeIOTimeout: TimeInterval = 5
private let firstUnreservedDescriptor: Int32 = 200
private let expectedAppName = "RemCTL Capability Host.app"
private let expectedBundleIdentifier = "net.macstories.remctl.capability-host"
private let hostActiveEnvironment = "REMCTL_CAPABILITY_HOST_ACTIVE"
private let hostAppEnvironment = "REMCTL_CAPABILITY_HOST_APP"
private let hostCDHashEnvironment = "REMCTL_CAPABILITY_HOST_CDHASH"
private let runtimeEnvironment = "REMCTL_CAPABILITY_RUNTIME"
private let remindersBundleIdentifier = "com.apple.reminders"

private struct VerifiedHost {
    let appPath: String
    let cdHash: String
    let runtimePath: String
}

private func signingCDHash(_ code: SecStaticCode) -> Data? {
    var information: CFDictionary?
    guard SecCodeCopySigningInformation(
        code,
        SecCSFlags(rawValue: kSecCSSigningInformation),
        &information
    ) == errSecSuccess,
          let information,
          let value = (information as NSDictionary)[kSecCodeInfoUnique] as? Data,
          value.count == 20
    else { return nil }
    return value
}

private func runningCDHash() -> Data? {
    var dynamicCode: SecCode?
    guard SecCodeCopySelf([], &dynamicCode) == errSecSuccess,
          let dynamicCode
    else { return nil }
    let dynamicAsStatic = unsafeBitCast(dynamicCode, to: SecStaticCode.self)
    return signingCDHash(dynamicAsStatic)
}

// Capture the launched identity before any on-disk bundle can be replaced.
private let launchedCDHash = runningCDHash()

private func canonicalPath(_ url: URL) -> String? {
    url.withUnsafeFileSystemRepresentation { suppliedPath in
        guard let suppliedPath,
              let resolvedPath = Darwin.realpath(suppliedPath, nil)
        else { return nil }
        defer { free(resolvedPath) }
        return String(cString: resolvedPath)
    }
}

private func hasExpectedBundleIdentifier(_ identifier: String?) -> Bool {
    if identifier == expectedBundleIdentifier { return true }
#if REMCTL_TESTING
    return identifier == "net.macstories.remctl.capability-host.runtime-tests"
#else
    return false
#endif
}

private func verifiedRunningHost() -> VerifiedHost? {
    guard let runningHash = launchedCDHash,
          hasExpectedBundleIdentifier(Bundle.main.bundleIdentifier),
          let canonicalAppPath = canonicalPath(Bundle.main.bundleURL),
          URL(fileURLWithPath: canonicalAppPath).lastPathComponent == expectedAppName
    else { return nil }
    let canonicalApp = URL(fileURLWithPath: canonicalAppPath, isDirectory: true)
    var staticCode: SecStaticCode?
    guard SecStaticCodeCreateWithPath(canonicalApp as CFURL, [], &staticCode) == errSecSuccess,
          let staticCode
    else { return nil }
    let validationFlags = SecCSFlags(rawValue:
        kSecCSCheckAllArchitectures | kSecCSStrictValidate | kSecCSCheckNestedCode
    )
    guard SecStaticCodeCheckValidity(staticCode, validationFlags, nil) == errSecSuccess,
          signingCDHash(staticCode) == runningHash,
          let resourceURL = Bundle.main.resourceURL,
          let resourcePath = canonicalPath(resourceURL)
    else { return nil }
    let runtimeURL = URL(fileURLWithPath: resourcePath, isDirectory: true)
        .appendingPathComponent("CapabilityRuntime", isDirectory: true)
    guard let runtimePath = canonicalPath(runtimeURL),
          runtimePath == runtimeURL.path
    else { return nil }
    var isDirectory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: runtimePath, isDirectory: &isDirectory),
          isDirectory.boolValue
    else { return nil }
    return VerifiedHost(
        appPath: canonicalAppPath,
        cdHash: runningHash.map { String(format: "%02x", $0) }.joined(),
        runtimePath: runtimePath
    )
}

private func resourceText(_ name: String) -> String? {
    guard let url = Bundle.main.url(forResource: name, withExtension: nil),
          let value = try? String(contentsOf: url, encoding: .utf8)
    else { return nil }
    let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? nil : trimmed
}

private func rootOnlyWheelIsSafe() -> Bool {
    if getgid() == 0 || getegid() == 0 { return false }
    let groupCount = getgroups(0, nil)
    guard groupCount >= 0 else { return false }
    var supplementaryGroups = [gid_t](repeating: 0, count: Int(groupCount))
    let loadedGroupCount = supplementaryGroups.withUnsafeMutableBufferPointer { buffer in
        getgroups(groupCount, buffer.baseAddress)
    }
    guard loadedGroupCount == groupCount,
          !supplementaryGroups.contains(0),
          let wheel = getgrgid(0),
          wheel.pointee.gr_gid == 0,
          let wheelName = wheel.pointee.gr_name,
          String(cString: wheelName) == "wheel",
          let root = getpwuid(0),
          root.pointee.pw_gid == 0
    else { return false }

    var memberNames: [String] = []
    if var member = wheel.pointee.gr_mem {
        while let name = member.pointee {
            memberNames.append(String(cString: name))
            member = member.advanced(by: 1)
        }
    }
    for name in memberNames {
        guard let account = getpwnam(name), account.pointee.pw_uid == 0 else { return false }
    }
    setpwent()
    defer { endpwent() }
    while let account = getpwent() {
        if account.pointee.pw_gid == 0 && account.pointee.pw_uid != 0 { return false }
    }
    return true
}

private let rootOnlyWheel = rootOnlyWheelIsSafe()

private func hasNoExtendedACL(_ path: String) -> Bool {
    errno = 0
    guard let acl = acl_get_file(path, ACL_TYPE_EXTENDED) else {
        return errno == ENOENT
    }
    defer { acl_free(UnsafeMutableRawPointer(acl)) }
    var entry: acl_entry_t? = nil
    return acl_get_entry(acl, Int32(ACL_FIRST_ENTRY.rawValue), &entry) == 0
}

private func protectedExecutable(_ path: String) -> Bool {
    guard path.hasPrefix("/"),
          let canonical = canonicalPath(URL(fileURLWithPath: path)),
          canonical == path,
          access(path, X_OK) == 0
    else { return false }

    var current = "/"
    var ancestry = [current]
    for component in URL(fileURLWithPath: path).pathComponents where component != "/" {
        current = URL(fileURLWithPath: current, isDirectory: true)
            .appendingPathComponent(component).path
        ancestry.append(current)
    }
    for component in ancestry {
        var metadata = stat()
        guard lstat(component, &metadata) == 0,
              metadata.st_uid == 0,
              metadata.st_mode & 0o002 == 0,
              (metadata.st_mode & 0o020 == 0 || (metadata.st_gid == 0 && rootOnlyWheel)),
              hasNoExtendedACL(component)
        else { return false }
    }
    var executableMetadata = stat()
    return stat(path, &executableMetadata) == 0
        && (executableMetadata.st_mode & S_IFMT) == S_IFREG
}

private func safeEnvironment(
    for host: VerifiedHost,
    includesNativeChannel: Bool = false
) -> [String: String] {
    var environment = [
        "HOME": FileManager.default.homeDirectoryForCurrentUser.path,
        "LANG": "en_US.UTF-8",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": "/private/tmp",
        archiveFDEnvironment: String(archiveDescriptor),
        hostActiveEnvironment: "1",
        hostAppEnvironment: host.appPath,
        hostCDHashEnvironment: host.cdHash,
        runtimeEnvironment: host.runtimePath,
    ]
    if includesNativeChannel {
        environment[nativeFDEnvironment] = String(nativeDescriptor)
    }
    return environment
}

private func canReadRemindersStore() -> Bool {
    let stores = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(
            "Library/Group Containers/group.com.apple.reminders/Container_v1/Stores",
            isDirectory: true
        )
    guard let entries = try? FileManager.default.contentsOfDirectory(
        at: stores,
        includingPropertiesForKeys: [.isRegularFileKey],
        options: [.skipsHiddenFiles]
    ) else { return false }
    for entry in entries where entry.lastPathComponent.hasPrefix("Data-")
        && entry.pathExtension == "sqlite" {
        do {
            let handle = try FileHandle(forReadingFrom: entry)
            defer { try? handle.close() }
            _ = try handle.read(upToCount: 1)
            return true
        } catch {
            continue
        }
    }
    return false
}

private func remindersAuthorizationStatus() -> String {
    let status = EKEventStore.authorizationStatus(for: .reminder)
    if status == .fullAccess { return "authorized" }
    if status == .writeOnly { return "writeOnly" }
    if status == .denied { return "denied" }
    if status == .restricted { return "restricted" }
    if status == .notDetermined { return "notDetermined" }
    return "unknown"
}

private func eventKitErrorStatus(_ error: Error) -> String {
    let value = error as NSError
    let truncatedDomain = String(value.domain.prefix(80))
    let sanitizedDomain = truncatedDomain.replacingOccurrences(
        of: "[^A-Za-z0-9._-]",
        with: "_",
        options: .regularExpression
    )
    return "error:\(sanitizedDomain.isEmpty ? "unknown" : sanitizedDomain):\(value.code)"
}

private func remindersRequestResult(granted: Bool, error: Error?) -> String {
    if let error { return eventKitErrorStatus(error) }
    return granted ? "authorized" : remindersAuthorizationStatus()
}

private func remindersIsRunning() -> Bool {
    !NSRunningApplication.runningApplications(
        withBundleIdentifier: remindersBundleIdentifier
    ).isEmpty
}

private func automationAuthorizationStatus(askUser: Bool) -> String {
    guard remindersIsRunning() else { return "targetNotRunning" }
    let target = NSAppleEventDescriptor(bundleIdentifier: remindersBundleIdentifier)
    let result = AEDeterminePermissionToAutomateTarget(
        target.aeDesc,
        typeWildCard,
        typeWildCard,
        askUser
    )
    switch result {
    case noErr: return "authorized"
    case OSStatus(errAEEventWouldRequireUserConsent): return "notDetermined"
    case OSStatus(errAEEventNotPermitted): return "denied"
    case OSStatus(procNotFound): return "targetNotRunning"
    default: return "error:\(result)"
    }
}

private final class AutomationStatusCache {
    private struct CachedStatus {
        let value: String
        let recordedAt: Date
        let refreshInterval: TimeInterval
    }

    private struct InFlightPreflight {
        let generation: UInt64
        let cacheRevision: UInt64
    }

    private let condition = NSCondition()
    private let waitBudget: TimeInterval
    private let statusTTL: TimeInterval
    private let targetNotRunningRetryInterval: TimeInterval
    private let queue: DispatchQueue
    private let now: () -> Date
    private let preflight: () -> String
    private var nextPreflightGeneration: UInt64 = 0
    private var cacheRevision: UInt64 = 0
    private var inFlightPreflight: InFlightPreflight?
    private var cachedStatus: CachedStatus?
    private var preflightStartCount = 0

    init(
        waitBudget: TimeInterval,
        statusTTL: TimeInterval,
        targetNotRunningRetryInterval: TimeInterval,
        queue: DispatchQueue,
        now: @escaping () -> Date = Date.init,
        preflight: @escaping () -> String
    ) {
        self.waitBudget = waitBudget
        self.statusTTL = statusTTL
        self.targetNotRunningRetryInterval = targetNotRunningRetryInterval
        self.queue = queue
        self.now = now
        self.preflight = preflight
    }

    func status() -> String {
        condition.lock()
        let existing = cachedStatus
        if let existing, isFresh(existing) {
            condition.unlock()
            return existing.value
        }
        if inFlightPreflight == nil {
            nextPreflightGeneration &+= 1
            preflightStartCount += 1
            let preflight = InFlightPreflight(
                generation: nextPreflightGeneration,
                cacheRevision: cacheRevision
            )
            inFlightPreflight = preflight
            queue.async { [weak self] in
                guard let self else { return }
                let status = self.preflight()
                self.condition.lock()
                if self.inFlightPreflight?.generation == preflight.generation {
                    self.inFlightPreflight = nil
                    if self.cacheRevision == preflight.cacheRevision {
                        self.cacheRevision &+= 1
                        self.cachedStatus = self.mergedStatus(
                            status,
                            with: self.cachedStatus,
                            recordedAt: self.now()
                        )
                    }
                }
                self.condition.broadcast()
                self.condition.unlock()
            }
        }
        if let existing {
            condition.unlock()
            return existing.value
        }
        guard !Thread.isMainThread, waitBudget > 0 else {
            condition.unlock()
            return "unknown"
        }
        let deadline = Date().addingTimeInterval(waitBudget)
        while cachedStatus == nil, condition.wait(until: deadline) {}
        let status = cachedStatus?.value ?? "unknown"
        condition.unlock()
        return status
    }

    func recordPermissionResult(_ status: String) -> String {
        guard !["timedOut", "cancelled", "promptUnavailable"].contains(status)
        else { return status }
        condition.lock()
        cacheRevision &+= 1
        let merged = mergedStatus(status, with: cachedStatus, recordedAt: now())
        cachedStatus = merged
        condition.broadcast()
        condition.unlock()
        return merged.value
    }

    private func isFresh(_ status: CachedStatus) -> Bool {
        now().timeIntervalSince(status.recordedAt) < status.refreshInterval
    }

    private func mergedStatus(
        _ candidate: String,
        with existing: CachedStatus?,
        recordedAt: Date
    ) -> CachedStatus {
        // Target liveness is not permission state. Keep a definitive TCC result,
        // but retry quickly so a relaunched Reminders process can be rechecked.
        if candidate == "targetNotRunning",
           let existing,
           ["authorized", "denied", "notDetermined"].contains(existing.value) {
            return CachedStatus(
                value: existing.value,
                recordedAt: recordedAt,
                refreshInterval: targetNotRunningRetryInterval
            )
        }
        return CachedStatus(
            value: candidate,
            recordedAt: recordedAt,
            refreshInterval: candidate == "targetNotRunning"
                ? targetNotRunningRetryInterval
                : statusTTL
        )
    }

#if REMCTL_TESTING
    func testingPreflightStartCount() -> Int {
        condition.lock()
        let count = preflightStartCount
        condition.unlock()
        return count
    }

    func testingWaitForIdle(timeout: TimeInterval) -> Bool {
        condition.lock()
        let deadline = Date().addingTimeInterval(timeout)
        while inFlightPreflight != nil {
            guard condition.wait(until: deadline) else {
                condition.unlock()
                return false
            }
        }
        condition.unlock()
        return true
    }
#endif
}

private let automationStatusCache = AutomationStatusCache(
    waitBudget: 0.2,
    statusTTL: 5,
    targetNotRunningRetryInterval: 1,
    queue: DispatchQueue(
        label: "net.macstories.remctl.capability-host.automation-status",
        qos: .utility
    ),
    preflight: { automationAuthorizationStatus(askUser: false) }
)

private func permissionStatusPayload(
    reminders: String? = nil,
    automation: String? = nil
) -> [String: Any] {
    [
        "status": "ok",
        "fullDiskAccess": canReadRemindersStore() ? "authorized" : "denied",
        "reminders": reminders ?? remindersAuthorizationStatus(),
        "automation": automation ?? automationStatusCache.status(),
        "automationTarget": remindersBundleIdentifier,
    ]
}

#if REMCTL_TESTING
private func emitPermissionStatus(
    reminders: String? = nil,
    automation: String? = nil
) -> Never {
    let payload = permissionStatusPayload(reminders: reminders, automation: automation)
    guard JSONSerialization.isValidJSONObject(payload),
          let data = try? JSONSerialization.data(
              withJSONObject: payload,
              options: [.sortedKeys]
          ),
          let output = String(data: data, encoding: .utf8)
    else { exit(70) }
    print(output)
    exit(0)
}
#endif

private enum PermissionPrompt {
    case reminders
    case automation
#if REMCTL_TESTING
    case windowTest
    case hangingTest
    case activationUnavailableTest
#endif
}

private struct PermissionPromptReadiness {
    let applicationActive: Bool
    let runningApplicationActive: Bool
    let frontmost: Bool
    let windowVisible: Bool
    let windowKey: Bool

    var isReady: Bool {
        applicationActive
            && runningApplicationActive
            && frontmost
            && windowVisible
            && windowKey
    }
}

private final class PermissionRequestGate {
    private enum State {
        case presenting
        case requesting
        case terminal
    }

    private var state = State.presenting

    var requestStarted: Bool { state == .requesting }
    var isTerminal: Bool { state == .terminal }

    func canContinue(with readiness: PermissionPromptReadiness) -> Bool {
        readiness.isReady && state == .presenting
    }

    func beginIfReady(_ readiness: PermissionPromptReadiness) -> Bool {
        guard canContinue(with: readiness) else { return false }
        state = .requesting
        return true
    }

    func cancelBeforeRequest() -> Bool {
        guard state == .presenting else { return false }
        state = .terminal
        return true
    }

    func finish() -> Bool {
        guard state != .terminal else { return false }
        state = .terminal
        return true
    }
}

private func shouldRestorePreviousApplication(
    previousPID: pid_t,
    previousIsTerminated: Bool,
    currentPID: pid_t,
    hostOwnsFocus: Bool
) -> Bool {
    hostOwnsFocus
        && previousPID > 0
        && previousPID != currentPID
        && !previousIsTerminated
}

private final class PermissionPromptController: NSObject, NSWindowDelegate {
    private let prompt: PermissionPrompt
    private let completion: (String) -> Void
    private let automationQueue = DispatchQueue(
        label: "net.macstories.remctl.capability-host.automation-prompt",
        qos: .userInitiated
    )
    private let requestGate = PermissionRequestGate()
    private var eventStore: EKEventStore?
    private var timeoutTimer: Timer?
    private var activationTimer: Timer?
    private var readinessTimer: Timer?
    private var permissionWindow: NSWindow?
    private var continueButton: NSButton?
    private var previousActiveApplication: NSRunningApplication?
    private var gateUsesRegularActivationPolicy = false
    private var finished = false
#if REMCTL_TESTING
    private var testingAutoClickScheduled = false
#endif

    init(prompt: PermissionPrompt, completion: @escaping (String) -> Void) {
        self.prompt = prompt
        self.completion = completion
    }

    func start() {
        capturePreviousActiveApplication()
        guard beginRegularActivationPolicy() else {
            finishBeforeRequest(with: "promptUnavailable")
            return
        }
        let window = makePermissionWindow()
        permissionWindow = window
        window.delegate = self
        installReadinessObservers()
        window.center()
        window.orderFrontRegardless()
        DispatchQueue.main.async { [weak self, weak window] in
            guard let self, !self.finished else { return }
            self.requestApplicationActivation()
            window?.makeKeyAndOrderFront(nil)
            self.updateContinueAvailability()
        }
        timeoutTimer = Timer.scheduledTimer(
            withTimeInterval: 290,
            repeats: false
        ) { [weak self] _ in
            // The native request must complete before its 295-second channel
            // deadline. This cannot revoke a TCC request already shown, but it
            // closes this gate and suppresses any later callback.
            self?.finish(with: "timedOut")
        }
        readinessTimer = Timer.scheduledTimer(
            withTimeInterval: 0.1,
            repeats: true
        ) { [weak self] _ in
            self?.updateContinueAvailability()
        }
        armActivationTimer()
        updateContinueAvailability()
    }

    func windowDidBecomeKey(_ notification: Notification) {
        updateContinueAvailability()
    }

    func windowDidResignKey(_ notification: Notification) {
        updateContinueAvailability()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if requestGate.cancelBeforeRequest() {
            finishTerminal(with: "cancelled")
        }
        // Once the native TCC request starts, closing this window cannot revoke
        // it. Keep the gate visible until macOS returns a result or it times out.
        return false
    }

    private func makePermissionWindow() -> NSWindow {
        let frame = NSRect(x: 0, y: 0, width: 460, height: 180)
        let window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "RemCTL Permissions"
        window.isReleasedWhenClosed = false

        let content = NSView(frame: frame)
        let heading = NSTextField(labelWithString: permissionHeading)
        heading.font = .systemFont(ofSize: 17, weight: .semibold)
        heading.frame = NSRect(x: 28, y: 112, width: 404, height: 28)
        let detail = NSTextField(wrappingLabelWithString: permissionDetail)
        detail.font = .systemFont(ofSize: 13)
        detail.textColor = .secondaryLabelColor
        detail.frame = NSRect(x: 28, y: 58, width: 404, height: 48)
        let button = NSButton(
            title: "Continue",
            target: self,
            action: #selector(continuePermissionRequest(_:))
        )
        button.bezelStyle = .rounded
        button.isEnabled = false
        button.frame = NSRect(x: 332, y: 20, width: 100, height: 30)
        content.addSubview(heading)
        content.addSubview(detail)
        content.addSubview(button)
        window.contentView = content
        continueButton = button
        return window
    }

    private var permissionHeading: String {
        switch prompt {
        case .reminders:
            return "Allow RemCTL to access Reminders"
        case .automation:
            return "Allow RemCTL to automate Reminders"
#if REMCTL_TESTING
        case .windowTest:
            return "RemCTL Permission Window Test"
        case .hangingTest:
            return "RemCTL Permission Shutdown Test"
        case .activationUnavailableTest:
            return "RemCTL Permission Activation Test"
#endif
        }
    }

    private var permissionDetail: String {
        switch prompt {
        case .reminders:
            return "Click Continue to ask macOS for full access to your reminders."
        case .automation:
            return "Click Continue to let RemCTL ask macOS to automate Reminders.app. Reminders.app may open."
#if REMCTL_TESTING
        case .windowTest:
            return "This test verifies the active, frontmost, key-window permission gate."
        case .hangingTest:
            return "This test keeps a simulated permission request open until the host shuts down."
        case .activationUnavailableTest:
            return "This test verifies bounded failure when the app cannot become active."
#endif
        }
    }

    private var activationTimeoutInterval: TimeInterval {
#if REMCTL_TESTING
        if case .activationUnavailableTest = prompt { return 0.05 }
#endif
        return 10
    }

    private func capturePreviousActiveApplication() {
        guard let application = NSWorkspace.shared.frontmostApplication,
              application.processIdentifier != getpid()
        else { return }
        previousActiveApplication = application
    }

    private func beginRegularActivationPolicy() -> Bool {
        if NSApp.activationPolicy() == .regular {
            gateUsesRegularActivationPolicy = true
            return true
        }
        guard NSApp.setActivationPolicy(.regular) else { return false }
        gateUsesRegularActivationPolicy = true
        return true
    }

    private func installReadinessObservers() {
        let center = NotificationCenter.default
        center.addObserver(
            self,
            selector: #selector(readinessDidChange(_:)),
            name: NSApplication.didBecomeActiveNotification,
            object: NSApp
        )
        center.addObserver(
            self,
            selector: #selector(readinessDidChange(_:)),
            name: NSApplication.didResignActiveNotification,
            object: NSApp
        )
    }

    @objc private func readinessDidChange(_ notification: Notification) {
        if NSApp.isActive { permissionWindow?.makeKeyAndOrderFront(nil) }
        updateContinueAvailability()
    }

    private func requestApplicationActivation() {
#if REMCTL_TESTING
        if case .activationUnavailableTest = prompt { return }
#endif
        NSApp.activate()
        _ = NSRunningApplication.current.activate(options: [.activateAllWindows])
        if !NSApp.isActive {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    private func currentReadiness() -> PermissionPromptReadiness {
#if REMCTL_TESTING
        if case .activationUnavailableTest = prompt {
            return PermissionPromptReadiness(
                applicationActive: false,
                runningApplicationActive: false,
                frontmost: false,
                windowVisible: permissionWindow?.isVisible == true,
                windowKey: false
            )
        }
#endif
        let running = NSRunningApplication.current
        return PermissionPromptReadiness(
            applicationActive: NSApp.isActive,
            runningApplicationActive: running.isActive,
            frontmost: NSWorkspace.shared.frontmostApplication?.processIdentifier == getpid(),
            windowVisible: permissionWindow?.isVisible == true,
            windowKey: permissionWindow?.isKeyWindow == true
        )
    }

    private func armActivationTimer() {
        guard activationTimer == nil, !requestGate.requestStarted, !finished else { return }
        activationTimer = Timer.scheduledTimer(
            withTimeInterval: activationTimeoutInterval,
            repeats: false
        ) { [weak self] _ in
            guard let self, !self.currentReadiness().isReady else { return }
#if REMCTL_TESTING
            if case .windowTest = self.prompt {
                self.finishBeforeRequest(with: self.windowTestResult(status: "promptUnavailable"))
                return
            }
#endif
            self.finishBeforeRequest(with: "promptUnavailable")
        }
    }

    private func updateContinueAvailability() {
        guard !finished else { return }
        let readiness = currentReadiness()
        continueButton?.isEnabled = requestGate.canContinue(with: readiness)
        if readiness.isReady {
            activationTimer?.invalidate()
            activationTimer = nil
#if REMCTL_TESTING
            scheduleTestingClickIfNeeded()
#endif
        } else {
            armActivationTimer()
        }
    }

    // The action rechecks every predicate before it changes the gate to requesting.
    // A visible-only window check is insufficient because a UIElement window can
    // be key before the app is active and frontmost.
    @objc private func continuePermissionRequest(_ sender: Any?) {
        guard requestGate.beginIfReady(currentReadiness()) else {
            continueButton?.isEnabled = false
            requestApplicationActivation()
            updateContinueAvailability()
            return
        }
        continueButton?.isEnabled = false
        permissionWindow?.standardWindowButton(.closeButton)?.isEnabled = false
        permissionWindow?.title = "RemCTL Permissions — Waiting for macOS"
        continueButton?.title = "Waiting…"
        activationTimer?.invalidate()
        activationTimer = nil
        beginCapabilityRequest()
    }

    private func beginCapabilityRequest() {
        switch prompt {
        case .reminders:
            requestReminders()
        case .automation:
            requestAutomation()
#if REMCTL_TESTING
        case .windowTest:
            finish(with: windowTestResult())
        case .hangingTest:
            writeHangingTestReadyState()
        case .activationUnavailableTest:
            finish(with: "capabilityConstructed")
#endif
        }
    }

    func cancel() {
        guard requestGate.cancelBeforeRequest() else { return }
        finishTerminal(with: "cancelled")
    }

    func shutdown() {
        guard requestGate.finish() else { return }
        finishTerminal(with: "cancelled", deliverCompletion: false)
    }

#if REMCTL_TESTING
    private func scheduleTestingClickIfNeeded() {
        guard !testingAutoClickScheduled else { return }
        switch prompt {
        case .windowTest:
            testingAutoClickScheduled = true
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if CommandLine.arguments.contains("--test-permission-window-close-output") {
                    self.permissionWindow?.performClose(nil)
                } else {
                    self.continueButton?.performClick(nil)
                }
            }
        case .hangingTest:
            testingAutoClickScheduled = true
            DispatchQueue.main.async { [weak self] in
                self?.continueButton?.performClick(nil)
            }
        case .reminders, .automation, .activationUnavailableTest:
            break
        }
    }

    private func writeHangingTestReadyState() {
        guard let path = resourceText("remctl-capability-host-test-prompt-ready-path")
        else { return }
        try? Data("ready".utf8).write(
            to: URL(fileURLWithPath: path),
            options: .atomic
        )
    }

    private func windowTestResult(status: String = "ok") -> String {
        let running = NSRunningApplication.current
        let payload: [String: Any] = [
            "status": status,
            "windowVisible": permissionWindow?.isVisible == true,
            "windowKey": permissionWindow?.isKeyWindow == true,
            "applicationActive": NSApp.isActive,
            "runningApplicationActive": running.isActive,
            "frontmostPID": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0,
            "requestStarted": requestGate.requestStarted,
            "activationPolicyDuringGate": NSApp.activationPolicy().rawValue,
            "windowTitle": permissionWindow?.title ?? "",
            "applicationFinishedLaunching": running.isFinishedLaunching,
            "bundleIdentifier": running.bundleIdentifier ?? "",
            "pid": running.processIdentifier,
        ]
        guard let data = try? JSONSerialization.data(
            withJSONObject: payload,
            options: [.sortedKeys]
        ), let output = String(data: data, encoding: .utf8)
        else { return "" }
        return output
    }
#endif

    private func requestReminders() {
        let existing = remindersAuthorizationStatus()
        guard existing == "notDetermined" else {
            finish(with: existing)
            return
        }
        let store = EKEventStore()
        eventStore = store
        store.requestFullAccessToReminders { [weak self] granted, error in
            let status = remindersRequestResult(granted: granted, error: error)
            DispatchQueue.main.async {
                self?.finish(with: status)
            }
        }
    }

    private func requestAutomation() {
        if remindersIsRunning() {
            determineAutomationAuthorization()
            return
        }
        guard let url = NSWorkspace.shared.urlForApplication(
            withBundleIdentifier: remindersBundleIdentifier
        ) else {
            finish(with: "targetNotRunning")
            return
        }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        configuration.createsNewApplicationInstance = false
        NSWorkspace.shared.openApplication(at: url, configuration: configuration) {
            [weak self] application, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                guard application != nil else {
                    self.finish(with: "targetNotRunning")
                    return
                }
                self.waitForRemindersToRun(deadline: Date().addingTimeInterval(5))
            }
        }
    }

    private func waitForRemindersToRun(deadline: Date) {
        if remindersIsRunning() {
            determineAutomationAuthorization()
            return
        }
        guard Date() < deadline else {
            finish(with: "targetNotRunning")
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            self?.waitForRemindersToRun(deadline: deadline)
        }
    }

    private func determineAutomationAuthorization() {
        automationQueue.async { [weak self] in
            let status = automationAuthorizationStatus(askUser: true)
            DispatchQueue.main.async {
                self?.finish(with: status)
            }
        }
    }

    private func finish(with status: String) {
        guard requestGate.finish() else { return }
        let effectiveStatus: String
        switch prompt {
        case .automation:
            effectiveStatus = automationStatusCache.recordPermissionResult(status)
        case .reminders:
            effectiveStatus = status
#if REMCTL_TESTING
        case .windowTest, .hangingTest, .activationUnavailableTest:
            effectiveStatus = status
#endif
        }
        finishTerminal(with: effectiveStatus)
    }

    private func finishBeforeRequest(with status: String) {
        guard requestGate.cancelBeforeRequest() else { return }
        finishTerminal(with: status)
    }

    private func finishTerminal(
        with status: String,
        deliverCompletion: Bool = true
    ) {
        guard !finished else { return }
        finished = true
        let readiness = currentReadiness()
        let hostOwnedFocus = readiness.applicationActive
            && readiness.runningApplicationActive
            && readiness.frontmost
        timeoutTimer?.invalidate()
        timeoutTimer = nil
        activationTimer?.invalidate()
        activationTimer = nil
        readinessTimer?.invalidate()
        readinessTimer = nil
        NotificationCenter.default.removeObserver(self)
        eventStore = nil
        permissionWindow?.orderOut(nil)
        permissionWindow?.close()
        permissionWindow = nil
        continueButton = nil
        restorePreviousActiveApplication(hostOwnedFocus: hostOwnedFocus)
        restoreAccessoryActivationPolicy()
#if REMCTL_TESTING
        writeTerminalTestingState(status: status)
#endif
        if deliverCompletion { completion(status) }
    }

    private func restoreAccessoryActivationPolicy() {
        guard gateUsesRegularActivationPolicy else { return }
        gateUsesRegularActivationPolicy = false
        if NSApp.activationPolicy() != .accessory {
            _ = NSApp.setActivationPolicy(.accessory)
        }
    }

    private func restorePreviousActiveApplication(hostOwnedFocus: Bool) {
        guard let previous = previousActiveApplication else { return }
        previousActiveApplication = nil
        guard shouldRestorePreviousApplication(
            previousPID: previous.processIdentifier,
            previousIsTerminated: previous.isTerminated,
            currentPID: getpid(),
            hostOwnsFocus: hostOwnedFocus
        ) else { return }
        NSApp.yieldActivation(to: previous)
        _ = previous.activate(options: [.activateAllWindows])
    }

#if REMCTL_TESTING
    private func writeTerminalTestingState(status: String) {
        guard let path = resourceText("remctl-capability-host-test-terminal-state-path")
        else { return }
        let payload: [String: Any] = [
            "status": status,
            "activationPolicy": NSApp.activationPolicy().rawValue,
            "permissionWindowVisible": permissionWindow?.isVisible == true,
            "terminal": requestGate.isTerminal,
        ]
        guard let data = try? JSONSerialization.data(
            withJSONObject: payload,
            options: [.sortedKeys]
        ) else { return }
        try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
    }
#endif
}

private func preparePermissionPromptApplication(_ application: NSApplication) -> Bool {
    if application.activationPolicy() == .accessory { return true }
    return application.setActivationPolicy(.accessory)
}

#if REMCTL_TESTING
private func permissionGateTestingPayload() -> [String: Any] {
    let ready = PermissionPromptReadiness(
        applicationActive: true,
        runningApplicationActive: true,
        frontmost: true,
        windowVisible: true,
        windowKey: true
    )
    let readinessGates: [String: PermissionPromptReadiness] = [
        "applicationInactive": PermissionPromptReadiness(
            applicationActive: false,
            runningApplicationActive: true,
            frontmost: true,
            windowVisible: true,
            windowKey: true
        ),
        "runningApplicationInactive": PermissionPromptReadiness(
            applicationActive: true,
            runningApplicationActive: false,
            frontmost: true,
            windowVisible: true,
            windowKey: true
        ),
        "notFrontmost": PermissionPromptReadiness(
            applicationActive: true,
            runningApplicationActive: true,
            frontmost: false,
            windowVisible: true,
            windowKey: true
        ),
        "windowHidden": PermissionPromptReadiness(
            applicationActive: true,
            runningApplicationActive: true,
            frontmost: true,
            windowVisible: false,
            windowKey: true
        ),
        "windowNotKey": PermissionPromptReadiness(
            applicationActive: true,
            runningApplicationActive: true,
            frontmost: true,
            windowVisible: true,
            windowKey: false
        ),
    ]
    let gate = PermissionRequestGate()
    let blocked = readinessGates.mapValues { !gate.canContinue(with: $0) }
    let readyAllowedInitially = gate.canContinue(with: ready)

    var requestCount = 0
    let requestCountBeforeClick = requestCount
    if gate.beginIfReady(ready) { requestCount += 1 }
    let requestCountAfterClick = requestCount
    if gate.beginIfReady(ready) { requestCount += 1 }
    let requestCountAfterSecondClick = requestCount
    let cancellationAfterRequest = gate.cancelBeforeRequest()
    let firstFinish = gate.finish()
    let secondFinish = gate.finish()

    let raceGate = PermissionRequestGate()
    var raceRequestCount = 0
    let staleReadiness = readinessGates["applicationInactive"]!
    if raceGate.beginIfReady(staleReadiness) { raceRequestCount += 1 }
    let raceRequestCountAfterLoss = raceRequestCount
    let raceRequiresFreshClick = raceGate.canContinue(with: ready)
    if raceGate.beginIfReady(ready) { raceRequestCount += 1 }

    let cancellationGate = PermissionRequestGate()
    var cancellationRequestCount = 0
    let cancellationAccepted = cancellationGate.cancelBeforeRequest()
    if cancellationGate.beginIfReady(ready) { cancellationRequestCount += 1 }

    let timeoutGate = PermissionRequestGate()
    var timeoutCompletionCount = 0
    let timeoutRequestStarted = timeoutGate.beginIfReady(ready)
    let timeoutAccepted = timeoutGate.finish()
    if timeoutAccepted { timeoutCompletionCount += 1 }
    let lateCallbackAccepted = timeoutGate.finish()
    if lateCallbackAccepted { timeoutCompletionCount += 1 }

    return [
        "ready": readyAllowedInitially,
        "blocked": blocked,
        "requestCountBeforeClick": requestCountBeforeClick,
        "requestCountAfterClick": requestCountAfterClick,
        "requestCountAfterSecondClick": requestCountAfterSecondClick,
        "cancellationAfterRequest": cancellationAfterRequest,
        "firstFinish": firstFinish,
        "secondFinish": secondFinish,
        "raceRequestCountAfterLoss": raceRequestCountAfterLoss,
        "raceRequiresFreshClick": raceRequiresFreshClick,
        "raceRequestCountAfterFreshClick": raceRequestCount,
        "cancellationAccepted": cancellationAccepted,
        "cancellationRequestCount": cancellationRequestCount,
        "cancellationTerminal": cancellationGate.isTerminal,
        "timeoutRequestStarted": timeoutRequestStarted,
        "timeoutAcceptedAfterRequest": timeoutAccepted,
        "lateCallbackAcceptedAfterTimeout": lateCallbackAccepted,
        "timeoutCompletionCount": timeoutCompletionCount,
        "timeoutTerminal": timeoutGate.isTerminal,
        "restoreEligible": shouldRestorePreviousApplication(
            previousPID: 10,
            previousIsTerminated: false,
            currentPID: 20,
            hostOwnsFocus: true
        ),
        "restoreRejectsTerminated": !shouldRestorePreviousApplication(
            previousPID: 10,
            previousIsTerminated: true,
            currentPID: 20,
            hostOwnsFocus: true
        ),
        "restoreRejectsSelf": !shouldRestorePreviousApplication(
            previousPID: 20,
            previousIsTerminated: false,
            currentPID: 20,
            hostOwnsFocus: true
        ),
        "restoreRejectsLostFocus": !shouldRestorePreviousApplication(
            previousPID: 10,
            previousIsTerminated: false,
            currentPID: 20,
            hostOwnsFocus: false
        ),
    ]
}

private final class AutomationStatusTestingBox: @unchecked Sendable {
    var status = ""
    var elapsed: TimeInterval = 0
}

private final class AutomationStatusTestingClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value = Date(timeIntervalSince1970: 1_000)

    func now() -> Date {
        lock.lock()
        let current = value
        lock.unlock()
        return current
    }

    func advance(by interval: TimeInterval) {
        lock.lock()
        value = value.addingTimeInterval(interval)
        lock.unlock()
    }
}

private final class AutomationStatusTestingSequence: @unchecked Sendable {
    private let lock = NSLock()
    private let values: [String]
    private var index = 0

    init(_ values: [String]) {
        self.values = values
    }

    func next() -> String {
        lock.lock()
        let value = values[min(index, values.count - 1)]
        index += 1
        lock.unlock()
        return value
    }
}

private func offMainAutomationStatus(
    _ cache: AutomationStatusCache
) -> (status: String, elapsed: TimeInterval) {
    let box = AutomationStatusTestingBox()
    let done = DispatchSemaphore(value: 0)
    DispatchQueue.global(qos: .utility).async {
        let started = Date()
        box.status = cache.status()
        box.elapsed = Date().timeIntervalSince(started)
        done.signal()
    }
    guard done.wait(timeout: .now() + 2) == .success else {
        return ("testTimedOut", 2)
    }
    return (box.status, box.elapsed)
}

private func automationStatusCacheTestingPayload() -> [String: Any] {
    let preflightStarted = DispatchSemaphore(value: 0)
    let releasePreflight = DispatchSemaphore(value: 0)
    let preflightCompleted = DispatchSemaphore(value: 0)
    let cache = AutomationStatusCache(
        waitBudget: 0.05,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-cache-test"),
        preflight: {
            preflightStarted.signal()
            _ = releasePreflight.wait(timeout: .now() + 2)
            preflightCompleted.signal()
            return "authorized"
        }
    )
    let first = offMainAutomationStatus(cache)
    _ = preflightStarted.wait(timeout: .now() + 1)
    let second = offMainAutomationStatus(cache)
    let pendingStartCount = cache.testingPreflightStartCount()
    releasePreflight.signal()
    _ = preflightCompleted.wait(timeout: .now() + 1)
    let resolved = offMainAutomationStatus(cache)

    let stalePreflightStarted = DispatchSemaphore(value: 0)
    let releaseStalePreflight = DispatchSemaphore(value: 0)
    let stalePreflightCompleted = DispatchSemaphore(value: 0)
    let authoritativeCache = AutomationStatusCache(
        waitBudget: 0.05,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-authoritative-test"),
        preflight: {
            stalePreflightStarted.signal()
            _ = releaseStalePreflight.wait(timeout: .now() + 2)
            stalePreflightCompleted.signal()
            return "authorized"
        }
    )
    let authoritativeInitial = offMainAutomationStatus(authoritativeCache)
    _ = stalePreflightStarted.wait(timeout: .now() + 1)
    _ = authoritativeCache.recordPermissionResult("denied")
    let authoritativeWhilePending = authoritativeCache.status()
    let authoritativePendingStartCount = authoritativeCache.testingPreflightStartCount()
    releaseStalePreflight.signal()
    _ = stalePreflightCompleted.wait(timeout: .now() + 1)
    _ = authoritativeCache.testingWaitForIdle(timeout: 1)
    let authoritativeFinal = offMainAutomationStatus(authoritativeCache)

    let ttlClock = AutomationStatusTestingClock()
    let ttlSequence = AutomationStatusTestingSequence(["authorized", "denied"])
    let ttlCache = AutomationStatusCache(
        waitBudget: 0.05,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-ttl-test"),
        now: { ttlClock.now() },
        preflight: { ttlSequence.next() }
    )
    let ttlInitial = offMainAutomationStatus(ttlCache)
    ttlClock.advance(by: 11)
    let ttlStaleWhileRefreshing = ttlCache.status()
    _ = ttlCache.testingWaitForIdle(timeout: 1)
    let ttlRefreshed = offMainAutomationStatus(ttlCache)

    let targetClock = AutomationStatusTestingClock()
    let targetSequence = AutomationStatusTestingSequence([
        "targetNotRunning", "authorized",
    ])
    let targetCache = AutomationStatusCache(
        waitBudget: 0.05,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-target-test"),
        now: { targetClock.now() },
        preflight: { targetSequence.next() }
    )
    let targetInitial = offMainAutomationStatus(targetCache)
    targetClock.advance(by: 1)
    let targetBeforeRetry = targetCache.status()
    let targetCountBeforeRetry = targetCache.testingPreflightStartCount()
    targetClock.advance(by: 2)
    let targetStaleWhileRefreshing = targetCache.status()
    _ = targetCache.testingWaitForIdle(timeout: 1)
    let targetRefreshed = offMainAutomationStatus(targetCache)

    let warmAuthorizedClock = AutomationStatusTestingClock()
    let warmAuthorizedSequence = AutomationStatusTestingSequence([
        "authorized", "targetNotRunning", "denied",
    ])
    let warmAuthorizedCache = AutomationStatusCache(
        waitBudget: 0.05,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-warm-authorized-test"),
        now: { warmAuthorizedClock.now() },
        preflight: { warmAuthorizedSequence.next() }
    )
    let warmAuthorizedInitial = offMainAutomationStatus(warmAuthorizedCache)
    warmAuthorizedClock.advance(by: 11)
    let warmAuthorizedWhileTargetStops = warmAuthorizedCache.status()
    _ = warmAuthorizedCache.testingWaitForIdle(timeout: 1)
    let warmAuthorizedAfterTargetStops = offMainAutomationStatus(warmAuthorizedCache)
    let warmAuthorizedCountAfterTargetStops = warmAuthorizedCache.testingPreflightStartCount()
    warmAuthorizedClock.advance(by: 3)
    let warmAuthorizedWhileDeniedRefreshes = warmAuthorizedCache.status()
    _ = warmAuthorizedCache.testingWaitForIdle(timeout: 1)
    let warmAuthorizedLaterDenied = offMainAutomationStatus(warmAuthorizedCache)

    let warmDeniedClock = AutomationStatusTestingClock()
    let warmDeniedSequence = AutomationStatusTestingSequence([
        "denied", "targetNotRunning",
    ])
    let warmDeniedCache = AutomationStatusCache(
        waitBudget: 0.05,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-warm-denied-test"),
        now: { warmDeniedClock.now() },
        preflight: { warmDeniedSequence.next() }
    )
    let warmDeniedInitial = offMainAutomationStatus(warmDeniedCache)
    warmDeniedClock.advance(by: 11)
    let warmDeniedWhileTargetStops = warmDeniedCache.status()
    _ = warmDeniedCache.testingWaitForIdle(timeout: 1)
    let warmDeniedAfterTargetStops = offMainAutomationStatus(warmDeniedCache)

    let warmNotDeterminedClock = AutomationStatusTestingClock()
    let warmNotDeterminedSequence = AutomationStatusTestingSequence([
        "notDetermined", "targetNotRunning",
    ])
    let warmNotDeterminedCache = AutomationStatusCache(
        waitBudget: 0.05,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-warm-not-determined-test"),
        now: { warmNotDeterminedClock.now() },
        preflight: { warmNotDeterminedSequence.next() }
    )
    let warmNotDeterminedInitial = offMainAutomationStatus(warmNotDeterminedCache)
    warmNotDeterminedClock.advance(by: 11)
    let warmNotDeterminedWhileTargetStops = warmNotDeterminedCache.status()
    _ = warmNotDeterminedCache.testingWaitForIdle(timeout: 1)
    let warmNotDeterminedAfterTargetStops = offMainAutomationStatus(warmNotDeterminedCache)

    let launchFailureCache = AutomationStatusCache(
        waitBudget: 0,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-launch-failure-test"),
        preflight: { "targetNotRunning" }
    )
    let launchFailureAuthorized = launchFailureCache.recordPermissionResult("authorized")
    let launchFailureAfterAuthorized = launchFailureCache.recordPermissionResult(
        "targetNotRunning"
    )
    let launchFailureTimedOut = launchFailureCache.recordPermissionResult("timedOut")
    let launchFailureCancelled = launchFailureCache.recordPermissionResult("cancelled")
    let launchFailurePromptUnavailable = launchFailureCache.recordPermissionResult(
        "promptUnavailable"
    )
    let launchFailureAfterTimedOut = launchFailureCache.status()

    let coldLaunchFailureCache = AutomationStatusCache(
        waitBudget: 0,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-cold-launch-failure-test"),
        preflight: { "authorized" }
    )
    let coldLaunchFailure = coldLaunchFailureCache.recordPermissionResult(
        "targetNotRunning"
    )

    let definitiveReplacementCache = AutomationStatusCache(
        waitBudget: 0,
        statusTTL: 10,
        targetNotRunningRetryInterval: 2,
        queue: DispatchQueue(label: "automation-status-definitive-replacement-test"),
        preflight: { "targetNotRunning" }
    )
    let definitiveReplacementAuthorized = definitiveReplacementCache
        .recordPermissionResult("authorized")
    let definitiveReplacementDenied = definitiveReplacementCache
        .recordPermissionResult("denied")
    let definitiveReplacementNotDetermined = definitiveReplacementCache
        .recordPermissionResult("notDetermined")

    return [
        "firstStatus": first.status,
        "firstElapsed": first.elapsed,
        "secondStatusWhilePending": second.status,
        "pendingStartCount": pendingStartCount,
        "resolvedStatus": resolved.status,
        "resolvedStartCount": cache.testingPreflightStartCount(),
        "authoritativeInitialStatus": authoritativeInitial.status,
        "authoritativeStatusWhilePreflightPending": authoritativeWhilePending,
        "authoritativePendingStartCount": authoritativePendingStartCount,
        "authoritativeFinalStatus": authoritativeFinal.status,
        "authoritativeStartCount": authoritativeCache.testingPreflightStartCount(),
        "ttlInitialStatus": ttlInitial.status,
        "ttlStaleWhileRefreshing": ttlStaleWhileRefreshing,
        "ttlRefreshedStatus": ttlRefreshed.status,
        "ttlRefreshStartCount": ttlCache.testingPreflightStartCount(),
        "targetInitialStatus": targetInitial.status,
        "targetBeforeRetryStatus": targetBeforeRetry,
        "targetCountBeforeRetry": targetCountBeforeRetry,
        "targetStaleWhileRefreshing": targetStaleWhileRefreshing,
        "targetRefreshedStatus": targetRefreshed.status,
        "targetRefreshStartCount": targetCache.testingPreflightStartCount(),
        "warmAuthorizedInitialStatus": warmAuthorizedInitial.status,
        "warmAuthorizedWhileTargetStops": warmAuthorizedWhileTargetStops,
        "warmAuthorizedAfterTargetStops": warmAuthorizedAfterTargetStops.status,
        "warmAuthorizedCountAfterTargetStops": warmAuthorizedCountAfterTargetStops,
        "warmAuthorizedWhileDeniedRefreshes": warmAuthorizedWhileDeniedRefreshes,
        "warmAuthorizedLaterDenied": warmAuthorizedLaterDenied.status,
        "warmAuthorizedFinalStartCount": warmAuthorizedCache.testingPreflightStartCount(),
        "warmDeniedInitialStatus": warmDeniedInitial.status,
        "warmDeniedWhileTargetStops": warmDeniedWhileTargetStops,
        "warmDeniedAfterTargetStops": warmDeniedAfterTargetStops.status,
        "warmNotDeterminedInitialStatus": warmNotDeterminedInitial.status,
        "warmNotDeterminedWhileTargetStops": warmNotDeterminedWhileTargetStops,
        "warmNotDeterminedAfterTargetStops": warmNotDeterminedAfterTargetStops.status,
        "launchFailureAuthorized": launchFailureAuthorized,
        "launchFailureAfterAuthorized": launchFailureAfterAuthorized,
        "launchFailureTimedOut": launchFailureTimedOut,
        "launchFailureCancelled": launchFailureCancelled,
        "launchFailurePromptUnavailable": launchFailurePromptUnavailable,
        "launchFailureAfterTimedOut": launchFailureAfterTimedOut,
        "coldLaunchFailure": coldLaunchFailure,
        "definitiveReplacementAuthorized": definitiveReplacementAuthorized,
        "definitiveReplacementDenied": definitiveReplacementDenied,
        "definitiveReplacementNotDetermined": definitiveReplacementNotDetermined,
    ]
}

private func emitTestingJSON(_ payload: [String: Any]) -> Never {
    guard JSONSerialization.isValidJSONObject(payload),
          let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
          let output = String(data: data, encoding: .utf8)
    else { exit(70) }
    print(output)
    exit(0)
}

private func windowTestCompletionResult(_ status: String) -> String {
    guard let data = status.data(using: .utf8),
          var payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else {
        let payload: [String: Any] = [
            "status": status,
            "activationPolicyAfterCompletion": NSApp.activationPolicy().rawValue,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
              let output = String(data: data, encoding: .utf8)
        else { return status }
        return output
    }
    payload["activationPolicyAfterCompletion"] = NSApp.activationPolicy().rawValue
    guard let outputData = try? JSONSerialization.data(
        withJSONObject: payload,
        options: [.sortedKeys]
    ), let output = String(data: outputData, encoding: .utf8)
    else { return status }
    return output
}

private final class OneShotPermissionApplicationDelegate: NSObject, NSApplicationDelegate {
    private let prompt: PermissionPrompt
    private let outputPath: String?
    private var controller: PermissionPromptController?

    init(prompt: PermissionPrompt, outputPath: String?) {
        self.prompt = prompt
        self.outputPath = outputPath
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let value = PermissionPromptController(prompt: prompt) { status in
            switch self.prompt {
            case .reminders:
                emitPermissionStatus(reminders: status)
            case .automation:
                emitPermissionStatus(automation: status)
#if REMCTL_TESTING
            case .windowTest:
                guard !status.isEmpty else { exit(70) }
                let output = windowTestCompletionResult(status)
                if let outputPath = self.outputPath {
                    do {
                        try Data(output.utf8).write(
                            to: URL(fileURLWithPath: outputPath),
                            options: .atomic
                        )
                    } catch {
                        exit(70)
                    }
                } else {
                    print(output)
                }
                exit(0)
            case .hangingTest:
                exit(70)
            case .activationUnavailableTest:
                emitTestingJSON([
                    "status": status,
                    "capabilityConstructed": status == "capabilityConstructed",
                    "activationPolicyAfterCompletion": NSApp.activationPolicy().rawValue,
                ])
#endif
            }
        }
        controller = value
        value.start()
    }
}

private func runPermissionPrompt(
    _ prompt: PermissionPrompt,
    outputPath: String? = nil
) -> Never {
    let application = NSApplication.shared
    guard preparePermissionPromptApplication(application) else {
        switch prompt {
        case .reminders:
            emitPermissionStatus(reminders: "promptUnavailable")
        case .automation:
            emitPermissionStatus(automation: "promptUnavailable")
#if REMCTL_TESTING
        case .windowTest:
            exit(70)
        case .hangingTest:
            exit(70)
        case .activationUnavailableTest:
            emitTestingJSON([
                "status": "promptUnavailable",
                "capabilityConstructed": false,
            ])
#endif
        }
    }
    let delegate = OneShotPermissionApplicationDelegate(
        prompt: prompt,
        outputPath: outputPath
    )
    application.delegate = delegate
    withExtendedLifetime(delegate) {
        application.run()
    }
    exit(70)
}
#endif

private func makeNativeSocketPair() -> (host: Int32, child: Int32)? {
    var descriptors = [Int32](repeating: -1, count: 2)
    guard socketpair(AF_UNIX, SOCK_STREAM, 0, &descriptors) == 0 else { return nil }
    guard fcntl(descriptors[0], F_SETFD, FD_CLOEXEC) == 0,
          fcntl(descriptors[1], F_SETFD, FD_CLOEXEC) == 0
    else {
        close(descriptors[0])
        close(descriptors[1])
        return nil
    }
    for index in descriptors.indices where
        descriptors[index] == archiveDescriptor || descriptors[index] == nativeDescriptor {
        guard let moved = relocateReservedDescriptor(descriptors[index]) else {
            descriptors.forEach { close($0) }
            return nil
        }
        descriptors[index] = moved
    }
    return (descriptors[0], descriptors[1])
}

private func relocateReservedDescriptor(_ descriptor: Int32) -> Int32? {
    guard descriptor == archiveDescriptor || descriptor == nativeDescriptor
    else { return descriptor }
    let moved = fcntl(descriptor, F_DUPFD_CLOEXEC, firstUnreservedDescriptor)
    guard moved >= firstUnreservedDescriptor else { return nil }
    close(descriptor)
    return moved
}

private func waitForDescriptor(
    _ descriptor: Int32,
    events: Int16,
    deadline: DispatchTime
) -> Bool {
    while true {
        let now = DispatchTime.now().uptimeNanoseconds
        guard deadline.uptimeNanoseconds > now else { return false }
        let remaining = deadline.uptimeNanoseconds - now
        var item = pollfd(fd: descriptor, events: events, revents: 0)
        let milliseconds = min(
            (remaining + 999_999) / 1_000_000,
            UInt64(Int32.max)
        )
        let result = poll(&item, 1, max(1, Int32(milliseconds)))
        if result < 0 && errno == EINTR { continue }
        let errors = Int16(POLLERR | POLLNVAL)
        guard result > 0, item.revents & errors == 0 else { return false }
        return item.revents & events != 0 || item.revents & Int16(POLLHUP) != 0
    }
}

private func readNativeBytes(
    descriptor: Int32,
    count: Int,
    deadline: DispatchTime
) -> Data? {
    var bytes = [UInt8](repeating: 0, count: count)
    var offset = 0
    while offset < count {
        guard waitForDescriptor(descriptor, events: Int16(POLLIN), deadline: deadline)
        else { return nil }
        let loaded = bytes.withUnsafeMutableBytes { buffer in
            read(descriptor, buffer.baseAddress!.advanced(by: offset), count - offset)
        }
        if loaded < 0 && errno == EINTR { continue }
        guard loaded > 0 else { return nil }
        offset += loaded
    }
    return Data(bytes)
}

private func readFirstNativeByte(descriptor: Int32) -> UInt8? {
    while true {
        var item = pollfd(fd: descriptor, events: Int16(POLLIN), revents: 0)
        let ready = poll(&item, 1, -1)
        if ready < 0 && errno == EINTR { continue }
        let errors = Int16(POLLERR | POLLNVAL)
        guard ready > 0, item.revents & errors == 0,
              item.revents & Int16(POLLIN | POLLHUP) != 0
        else { return nil }
        var byte: UInt8 = 0
        let loaded = read(descriptor, &byte, 1)
        if loaded < 0 && errno == EINTR { continue }
        return loaded == 1 ? byte : nil
    }
}

private func writeNativeBytes(
    descriptor: Int32,
    data: Data,
    deadline: DispatchTime
) -> Bool {
    var offset = 0
    while offset < data.count {
        guard waitForDescriptor(descriptor, events: Int16(POLLOUT), deadline: deadline)
        else { return false }
        let written = data.withUnsafeBytes { buffer in
            write(descriptor, buffer.baseAddress!.advanced(by: offset), data.count - offset)
        }
        if written < 0 && errno == EINTR { continue }
        guard written > 0 else { return false }
        offset += written
    }
    return true
}

private func readNativeRequest(descriptor: Int32) -> [String: Any]? {
    guard let firstHeaderByte = readFirstNativeByte(descriptor: descriptor) else { return nil }
    let deadline = DispatchTime.now() + nativeIOTimeout
    guard let remainingHeader = readNativeBytes(
        descriptor: descriptor,
        count: 3,
        deadline: deadline
    )
    else { return nil }
    let headerBytes = [firstHeaderByte] + [UInt8](remainingHeader)
    let length = Int(headerBytes[0]) << 24
        | Int(headerBytes[1]) << 16
        | Int(headerBytes[2]) << 8
        | Int(headerBytes[3])
    guard length > 0, length <= nativeRequestLimit,
          let body = readNativeBytes(descriptor: descriptor, count: length, deadline: deadline),
          let value = try? JSONSerialization.jsonObject(with: body),
          let request = value as? [String: Any]
    else { return nil }
    return request
}

private func writeNativeResponse(descriptor: Int32, response: [String: Any]) -> Bool {
    guard JSONSerialization.isValidJSONObject(response),
          let body = try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]),
          !body.isEmpty,
          body.count <= nativeResponseLimit
    else { return false }
    var size = UInt32(body.count).bigEndian
    let header = Data(bytes: &size, count: MemoryLayout<UInt32>.size)
    let deadline = DispatchTime.now() + nativeIOTimeout
    return writeNativeBytes(descriptor: descriptor, data: header, deadline: deadline)
        && writeNativeBytes(descriptor: descriptor, data: body, deadline: deadline)
}

private func nativeErrorResponse(code: String, message: String) -> [String: Any] {
    [
        "protocolVersion": nativeProtocolVersion,
        "status": "error",
        "code": String(code.prefix(80)),
        "message": String(message.prefix(500)),
    ]
}

private func nativeProtocolVersionIsValid(_ value: Any?) -> Bool {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          !CFNumberIsFloatType(number),
          number.intValue == nativeProtocolVersion
    else { return false }
    return true
}

private final class NativePermissionServer {
    private final class ResponseBox {
        var value: [String: Any]?
    }

    private let descriptor: Int32
    private let requestHandler: (PermissionPrompt, @escaping ([String: Any]) -> Void) -> Void
    private let disconnected: () -> Void
    private let queue = DispatchQueue(
        label: "net.macstories.remctl.capability-host.native-protocol",
        qos: .userInitiated
    )
    private let stateLock = NSLock()
    private var stopped = false

    init(
        descriptor: Int32,
        requestHandler: @escaping (PermissionPrompt, @escaping ([String: Any]) -> Void) -> Void,
        disconnected: @escaping () -> Void
    ) {
        self.descriptor = descriptor
        self.requestHandler = requestHandler
        self.disconnected = disconnected
    }

    func start() {
        queue.async { [weak self] in self?.serve() }
    }

    func stop() {
        stateLock.lock()
        let shouldClose = !stopped
        stopped = true
        stateLock.unlock()
        if shouldClose {
            shutdown(descriptor, SHUT_RDWR)
            close(descriptor)
        }
    }

    private func isStopped() -> Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return stopped
    }

    private func validatedOperation(_ request: [String: Any]) -> String? {
        guard Set(request.keys) == ["protocolVersion", "operation"],
              nativeProtocolVersionIsValid(request["protocolVersion"]),
              let operation = request["operation"] as? String,
              ["permissionStatus", "requestReminders", "requestAutomation"].contains(operation)
        else { return nil }
        return operation
    }

    private func successResponse(_ permissions: [String: Any]) -> [String: Any] {
        [
            "protocolVersion": nativeProtocolVersion,
            "status": "ok",
            "permissions": permissions,
        ]
    }

    private func responseForOperation(_ operation: String) -> [String: Any] {
        if operation == "permissionStatus" {
            return successResponse(permissionStatusPayload())
        }
        let box = ResponseBox()
        let semaphore = DispatchSemaphore(value: 0)
        let prompt: PermissionPrompt = operation == "requestReminders" ? .reminders : .automation
        DispatchQueue.main.async { [requestHandler] in
            requestHandler(prompt) { permissions in
                box.value = permissions
                semaphore.signal()
            }
        }
        guard semaphore.wait(timeout: .now() + 295) == .success,
              let permissions = box.value
        else {
            return nativeErrorResponse(
                code: "native_permission_timeout",
                message: "Native permission request timed out"
            )
        }
        return successResponse(permissions)
    }

    private func serve() {
        while !isStopped() {
            guard let request = readNativeRequest(descriptor: descriptor) else { break }
            let response: [String: Any]
            if let operation = validatedOperation(request) {
                response = responseForOperation(operation)
            } else {
                response = nativeErrorResponse(
                    code: "invalid_native_request",
                    message: "Native permission request is invalid"
                )
            }
            guard writeNativeResponse(descriptor: descriptor, response: response) else { break }
        }
        if !isStopped() {
            DispatchQueue.main.async { [disconnected] in disconnected() }
        }
    }
}

private func embeddedArchive() -> UnsafeRawBufferPointer? {
    guard let header = _dyld_get_image_header(0) else { return nil }
    let header64 = UnsafeRawPointer(header).assumingMemoryBound(to: mach_header_64.self)
    var size: UInt = 0
    guard let bytes = getsectiondata(header64, "__TEXT", "__rctl_pyz", &size),
          size > 0
    else { return nil }
    return UnsafeRawBufferPointer(start: bytes, count: Int(size))
}

private func materializeArchive() -> Int32? {
    guard let archive = embeddedArchive(), let base = archive.baseAddress else { return nil }
    var template = Array("/private/tmp/remctl-runtime.XXXXXX".utf8CString)
    var descriptor = template.withUnsafeMutableBufferPointer { buffer -> Int32 in
        guard let name = buffer.baseAddress else { return -1 }
        let result = mkstemp(name)
        if result >= 0 { _ = unlink(name) }
        return result
    }
    guard descriptor >= 0 else { return nil }
#if REMCTL_TESTING
    if resourceText("remctl-capability-host-test-force-archive-fd-199") == "1",
       descriptor != nativeDescriptor {
        let forced = dup2(descriptor, nativeDescriptor)
        guard forced == nativeDescriptor else {
            close(descriptor)
            return nil
        }
        close(descriptor)
        descriptor = forced
    }
#endif
    if descriptor == archiveDescriptor || descriptor == nativeDescriptor {
        guard let moved = relocateReservedDescriptor(descriptor) else {
            close(descriptor)
            return nil
        }
        descriptor = moved
    }
    guard descriptor >= 0,
          fchmod(descriptor, 0o600) == 0,
          fcntl(descriptor, F_SETFD, FD_CLOEXEC) == 0
    else {
        if descriptor >= 0 { close(descriptor) }
        return nil
    }
    var offset = 0
    while offset < archive.count {
        let count = write(descriptor, base.advanced(by: offset), archive.count - offset)
        if count < 0 && errno == EINTR { continue }
        guard count > 0 else {
            close(descriptor)
            return nil
        }
        offset += count
    }
    var metadata = stat()
    guard lseek(descriptor, 0, SEEK_SET) == 0,
          fstat(descriptor, &metadata) == 0,
          (metadata.st_mode & S_IFMT) == S_IFREG,
          metadata.st_nlink == 0,
          metadata.st_size == archive.count
    else {
        close(descriptor)
        return nil
    }
    return descriptor
}

private func spawnArchivedPython(
    pythonPath: String,
    host: VerifiedHost,
    arguments: [String],
    nativeChannel: Int32? = nil
) -> pid_t? {
    guard protectedExecutable(pythonPath),
          let archive = materializeArchive()
    else { return nil }
    defer { close(archive) }
    var actions: posix_spawn_file_actions_t? = nil
    var attributes: posix_spawnattr_t? = nil
    guard posix_spawn_file_actions_init(&actions) == 0,
          posix_spawnattr_init(&attributes) == 0
    else { return nil }
    defer {
        posix_spawn_file_actions_destroy(&actions)
        posix_spawnattr_destroy(&attributes)
    }
    let chdirResult = posix_spawn_file_actions_addchdir_np(&actions, host.runtimePath)
    let nativeChannelActionsReady: Bool
    if let nativeChannel {
        nativeChannelActionsReady = posix_spawn_file_actions_adddup2(
            &actions, nativeChannel, nativeDescriptor
        ) == 0 && (nativeChannel == nativeDescriptor
            || posix_spawn_file_actions_addclose(&actions, nativeChannel) == 0)
    } else {
        nativeChannelActionsReady = true
    }
    guard posix_spawn_file_actions_addopen(
        &actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0
    ) == 0,
          posix_spawn_file_actions_addopen(
              &actions, STDOUT_FILENO, "/dev/null", O_WRONLY, 0
          ) == 0,
          posix_spawn_file_actions_addopen(
              &actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0
          ) == 0,
          posix_spawn_file_actions_adddup2(&actions, archive, archiveDescriptor) == 0,
          posix_spawn_file_actions_addclose(&actions, archive) == 0,
          nativeChannelActionsReady,
          chdirResult == 0
    else { return nil }
    let flags = Int16(POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETPGROUP)
    guard posix_spawnattr_setflags(&attributes, flags) == 0,
          posix_spawnattr_setpgroup(&attributes, 0) == 0
    else { return nil }

    let argumentStrings = [
        pythonPath, "-I", "-S", "/dev/fd/\(archiveDescriptor)",
    ] + arguments
    let environmentStrings = safeEnvironment(
        for: host,
        includesNativeChannel: nativeChannel != nil
    )
        .map { "\($0.key)=\($0.value)" }
        .sorted()
    let argumentPointers = argumentStrings.map { strdup($0)! }
    let environmentPointers = environmentStrings.map { strdup($0)! }
    defer {
        argumentPointers.forEach { free($0) }
        environmentPointers.forEach { free($0) }
    }
    var argv: [UnsafeMutablePointer<CChar>?] = argumentPointers.map(Optional.some) + [nil]
    var envp: [UnsafeMutablePointer<CChar>?] = environmentPointers.map(Optional.some) + [nil]
    var pid: pid_t = 0
    let spawnResult = argv.withUnsafeMutableBufferPointer { argvBuffer in
        envp.withUnsafeMutableBufferPointer { envBuffer in
            posix_spawn(
                &pid,
                pythonPath,
                &actions,
                &attributes,
                argvBuffer.baseAddress!,
                envBuffer.baseAddress!
            )
        }
    }
    return spawnResult == 0 ? pid : nil
}

private func waitForChild(_ pid: pid_t) -> Int32 {
    var status: Int32 = 0
    while waitpid(pid, &status, 0) < 0 {
        if errno == EINTR { continue }
        return 70
    }
    let signalNumber = status & 0x7f
    if signalNumber == 0 { return (status >> 8) & 0xff }
    return 128 + signalNumber
}

private final class CapabilityHostApplicationDelegate: NSObject, NSApplicationDelegate {
    private let host: VerifiedHost
    private let socketPath: String
    private var brokerPID: pid_t?
    private var nativeServer: NativePermissionServer?
    private var permissionController: PermissionPromptController?
    private var terminationSource: DispatchSourceSignal?
    private var interruptSource: DispatchSourceSignal?
    private var stopping = false

    init(host: VerifiedHost, socketPath: String) {
        self.host = host
        self.socketPath = socketPath
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard preparePermissionPromptApplication(NSApp),
              let pythonPath = resourceText("remctl-capability-python-path"),
              protectedExecutable(pythonPath),
              let pair = makeNativeSocketPair()
        else { exit(70) }

        guard let pid = spawnArchivedPython(
            pythonPath: pythonPath,
            host: host,
            arguments: ["service", "--socket", socketPath],
            nativeChannel: pair.child
        ) else {
            close(pair.host)
            close(pair.child)
            exit(70)
        }
        close(pair.child)
        brokerPID = pid

        let server = NativePermissionServer(
            descriptor: pair.host,
            requestHandler: { [weak self] prompt, completion in
                self?.beginPermissionRequest(prompt, completion: completion)
            },
            disconnected: { [weak self] in
                self?.stopAfterProtocolFailure()
            }
        )
        nativeServer = server
        server.start()
        installSignalHandlers(for: pid)
        watchBroker(pid)
        writeTestingStateIfConfigured()
    }

    func applicationWillTerminate(_ notification: Notification) {
        permissionController?.shutdown()
        permissionController = nil
        nativeServer?.stop()
        if let pid = brokerPID { _ = kill(-pid, SIGTERM) }
    }

    private func beginPermissionRequest(
        _ prompt: PermissionPrompt,
        completion: @escaping ([String: Any]) -> Void
    ) {
        guard permissionController == nil else {
            completion(permissionStatusPayload())
            return
        }
        var controllerPrompt = prompt
#if REMCTL_TESTING
        if case .reminders = prompt,
           resourceText("remctl-capability-host-test-hang-permission") == "1" {
            controllerPrompt = .hangingTest
        }
#endif
        let controller = PermissionPromptController(prompt: controllerPrompt) { [weak self] status in
            guard let self else { return }
            let permissions: [String: Any]
            switch prompt {
            case .reminders:
                permissions = permissionStatusPayload(reminders: status)
            case .automation:
                permissions = permissionStatusPayload(automation: status)
#if REMCTL_TESTING
            case .windowTest:
                permissions = permissionStatusPayload()
            case .hangingTest:
                permissions = permissionStatusPayload()
            case .activationUnavailableTest:
                permissions = permissionStatusPayload()
#endif
            }
            self.permissionController = nil
            completion(permissions)
        }
        permissionController = controller
        controller.start()
    }

    private func installSignalHandlers(for pid: pid_t) {
        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        let termination = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
        let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
        termination.setEventHandler { [weak self] in
            self?.handleSignal(SIGTERM, brokerPID: pid)
        }
        interrupt.setEventHandler { [weak self] in
            self?.handleSignal(SIGINT, brokerPID: pid)
        }
        termination.resume()
        interrupt.resume()
        terminationSource = termination
        interruptSource = interrupt
    }

    private func handleSignal(_ signalNumber: Int32, brokerPID pid: pid_t) {
        nativeServer?.stop()
        DispatchQueue.main.sync { [weak self] in
            self?.permissionController?.shutdown()
            self?.permissionController = nil
        }
        _ = kill(-pid, signalNumber)
    }

    private func watchBroker(_ pid: pid_t) {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let status = waitForChild(pid)
            DispatchQueue.main.async {
                self?.finish(status: status)
            }
        }
    }

    private func stopAfterProtocolFailure() {
        guard !stopping else { return }
        stopping = true
        if let pid = brokerPID { _ = kill(-pid, SIGTERM) }
    }

    private func finish(status: Int32) {
        guard !stopping || brokerPID != nil else { return }
        stopping = true
        permissionController?.shutdown()
        permissionController = nil
        brokerPID = nil
        nativeServer?.stop()
        nativeServer = nil
        terminationSource?.cancel()
        interruptSource?.cancel()
        exit(status)
    }

    private func writeTestingStateIfConfigured() {
#if REMCTL_TESTING
        guard let path = resourceText("remctl-capability-host-test-state-path") else { return }
        let running = NSRunningApplication.current
        let payload: [String: Any] = [
            "pid": running.processIdentifier,
            "bundleIdentifier": running.bundleIdentifier ?? "",
            "applicationFinishedLaunching": running.isFinishedLaunching,
            "activationPolicy": NSApp.activationPolicy().rawValue,
            "windowCount": NSApp.windows.filter(\.isVisible).count,
            "brokerPID": brokerPID ?? 0,
        ]
        guard let data = try? JSONSerialization.data(
            withJSONObject: payload,
            options: [.sortedKeys]
        ) else { return }
        try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
#endif
    }
}

private func runService(
    host: VerifiedHost,
    socketPath: String
) -> Never {
    let application = NSApplication.shared
    guard preparePermissionPromptApplication(application) else { exit(70) }
    let delegate = CapabilityHostApplicationDelegate(host: host, socketPath: socketPath)
    application.delegate = delegate
    withExtendedLifetime(delegate) {
        application.run()
    }
    exit(70)
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard let verifiedHost = verifiedRunningHost() else { exit(65) }

#if REMCTL_TESTING
if arguments == ["--test-native-protocol-version"] {
    let payload: [String: Any] = [
        "integer": nativeProtocolVersionIsValid(NSNumber(value: 1)),
        "fractional": nativeProtocolVersionIsValid(NSNumber(value: 1.5)),
        "boolean": nativeProtocolVersionIsValid(NSNumber(value: true)),
    ]
    guard let data = try? JSONSerialization.data(
        withJSONObject: payload,
        options: [.sortedKeys]
    ), let output = String(data: data, encoding: .utf8)
    else { exit(70) }
    print(output)
    exit(0)
}
if arguments == ["--test-eventkit-error-status"] {
    let error = NSError(domain: "EK Error/Unsafe", code: 37)
    print(remindersRequestResult(granted: false, error: error))
    exit(0)
}
if arguments == ["--test-permission-window"] {
    runPermissionPrompt(.windowTest)
}
if arguments.count == 2, arguments[0] == "--test-permission-window-output" {
    runPermissionPrompt(.windowTest, outputPath: arguments[1])
}
if arguments.count == 2, arguments[0] == "--test-permission-window-close-output" {
    runPermissionPrompt(.windowTest, outputPath: arguments[1])
}
if arguments == ["--test-permission-gate"] {
    emitTestingJSON(permissionGateTestingPayload())
}
if arguments == ["--test-automation-status-cache"] {
    emitTestingJSON(automationStatusCacheTestingPayload())
}
if arguments == ["--test-activation-unavailable"] {
    runPermissionPrompt(.activationUnavailableTest)
}
if arguments.count == 3, arguments[0] == "--test-verify-running-host" {
    let ready = URL(fileURLWithPath: arguments[1])
    let resume = URL(fileURLWithPath: arguments[2])
    try? Data("ready".utf8).write(to: ready, options: .atomic)
    let deadline = Date().addingTimeInterval(10)
    while Date() < deadline && !FileManager.default.fileExists(atPath: resume.path) {
        Thread.sleep(forTimeInterval: 0.01)
    }
    guard FileManager.default.fileExists(atPath: resume.path) else { exit(70) }
    exit(verifiedRunningHost() == nil ? 65 : 0)
}
#endif

guard arguments.count == 3,
      arguments[0] == "--run-capability-host",
      arguments[1] == "--socket",
      let socketPath = resourceText("remctl-capability-host-socket-path"),
      arguments[2] == socketPath
else { exit(64) }
runService(host: verifiedHost, socketPath: socketPath)
