import Foundation
import EventKit
import CoreLocation

// MARK: - JSON Models

struct Command: Decodable {
    let action: String
    let id: String?
    let title: String?
    let newTitle: String?
    let list: String?
    let listId: String?
    let due: String?
    let priority: Int?
    let notes: String?
    let url: String?
    let flagged: Bool?
    let recurrence: RecurrenceSpec?
    let alarm: String?
    let allDay: Bool?
    let clearAlarms: Bool?
    let locationTitle: String?
    let latitude: Double?
    let longitude: Double?
    let radius: Double?
    let proximity: String?
    let color: String?
    let completionDate: String?
    let readMode: String?
    let query: String?
    let completed: Bool?
    let days: Int?
    let includeOverdue: Bool?
    let limit: Int?
    let address: String?
    let timeoutSeconds: Double?
}

struct RecurrenceSpec: Decodable {
    let frequency: String
    let interval: Int?
    let daysOfWeek: [Int]?
    let weekNumbers: [Int]?
    let daysOfMonth: [Int]?
    let monthsOfYear: [Int]?
    let daysOfYear: [Int]?
    let weeksOfYear: [Int]?
    let setPositions: [Int]?
    let count: Int?
    let end: String?
}

// MARK: - Output helpers

func output(_ dict: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: dict),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    }
}

func fail(_ message: String) -> Never {
    output(["status": "error", "message": message])
    exit(1)
}

// MARK: - Date parsing

let isoFormatter: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

let isoDateOnly: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withFullDate]
    return f
}()

/// Handles naive ISO datetimes like "2026-03-28T15:00:00" (no timezone)
/// by interpreting them as local time — the intuitive behavior for a CLI.
let localFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    f.timeZone = TimeZone.current
    f.locale = Locale(identifier: "en_US_POSIX")
    return f
}()

let localDateTimeShort: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm"
    f.timeZone = TimeZone.current
    f.locale = Locale(identifier: "en_US_POSIX")
    return f
}()

func parseISO(_ s: String) -> Date? {
    // 1. Full ISO 8601 with timezone (e.g., "2026-03-28T15:00:00Z" or "+02:00")
    if let d = isoFormatter.date(from: s) { return d }
    // 2. Naive datetime as local time (e.g., "2026-03-28T15:00:00")
    if let d = localFormatter.date(from: s) { return d }
    // 3. Shorter format (e.g., "2026-03-28 15:00")
    if let d = localDateTimeShort.date(from: s) { return d }
    // 4. Date-only as local midnight (e.g., "2026-03-28")
    if let d = isoDateOnly.date(from: s) {
        // Convert from UTC midnight to local midnight
        let cal = Calendar.current
        let utc = TimeZone(identifier: "UTC") ?? TimeZone(secondsFromGMT: 0)!
        let comps = cal.dateComponents(in: utc, from: d)
        var local = DateComponents()
        local.year = comps.year; local.month = comps.month; local.day = comps.day
        local.hour = 0; local.minute = 0; local.second = 0
        local.timeZone = TimeZone.current
        return cal.date(from: local) ?? d
    }
    return nil
}

// MARK: - Alarm parsing

func parseAlarm(_ s: String) -> EKAlarm? {
    // Relative: -15m, -1h, -1d
    if s.hasPrefix("-") {
        let body = s.dropFirst()
        guard let numEnd = body.lastIndex(where: { $0.isNumber }),
              let value = Double(body[body.startIndex...numEnd]) else { return nil }
        let unit = body[body.index(after: numEnd)...]
        let seconds: Double
        switch unit {
        case "m": seconds = value * 60
        case "h": seconds = value * 3600
        case "d": seconds = value * 86400
        default: return nil
        }
        return EKAlarm(relativeOffset: -seconds)
    }
    // Absolute ISO 8601
    if let date = parseISO(s) {
        return EKAlarm(absoluteDate: date)
    }
    return nil
}

// MARK: - Recurrence

func buildRecurrenceRule(_ spec: RecurrenceSpec) -> EKRecurrenceRule? {
    let freq: EKRecurrenceFrequency
    switch spec.frequency {
    case "daily":   freq = .daily
    case "weekly":  freq = .weekly
    case "monthly": freq = .monthly
    case "yearly":  freq = .yearly
    default: return nil
    }

    let interval = spec.interval ?? 1
    guard interval > 0 else { return nil }

    var daysOfWeek: [EKRecurrenceDayOfWeek]?
    if let days = spec.daysOfWeek {
        // Input: 1=Sun, 2=Mon ... 7=Sat → EKWeekday raw values match
        guard !days.isEmpty, days.allSatisfy({ 1...7 ~= $0 }) else { return nil }

        // Week numbers pin a weekday to the Nth week of the period (4 = 4th Friday,
        // -1 = last Friday); 0 means unpinned. Validate here: EKRecurrenceDayOfWeek
        // raises an uncatchable NSException on an out-of-range or wrong-frequency
        // week number, so a bad value must never reach it.
        var weekNumbers: [Int]?
        if let numbers = spec.weekNumbers, !numbers.isEmpty {
            guard numbers.count == days.count else { return nil }
            let bounds: ClosedRange<Int>
            switch freq {
            case .monthly: bounds = -5...5
            case .yearly:  bounds = -53...53
            default:
                // daily/weekly accept no pinning at all
                guard numbers.allSatisfy({ $0 == 0 }) else { return nil }
                bounds = 0...0
            }
            guard numbers.allSatisfy({ $0 == 0 || bounds ~= $0 }) else { return nil }
            weekNumbers = numbers
        }

        let mapped = days.enumerated().compactMap { index, day -> EKRecurrenceDayOfWeek? in
            guard let weekday = EKWeekday(rawValue: day) else { return nil }
            let number = weekNumbers?[index] ?? 0
            guard number != 0 else { return EKRecurrenceDayOfWeek(weekday) }
            return EKRecurrenceDayOfWeek(dayOfTheWeek: weekday, weekNumber: number)
        }
        guard mapped.count == days.count else { return nil }
        daysOfWeek = mapped
    }

    var daysOfMonth: [NSNumber]?
    if let days = spec.daysOfMonth {
        guard !days.isEmpty, days.allSatisfy({ $0 != 0 && -31...31 ~= $0 }) else { return nil }
        daysOfMonth = days.map { NSNumber(value: $0) }
    }

    for (values, bound, allowsNegative) in [
        (spec.monthsOfYear, 12, false), (spec.daysOfYear, 366, true),
        (spec.weeksOfYear, 53, true), (spec.setPositions, 366, true)
    ] {
        if let values = values {
            let lower = allowsNegative ? -bound : 1
            guard !values.isEmpty, values.allSatisfy({ $0 != 0 && lower...bound ~= $0 }) else { return nil }
        }
    }
    var end: EKRecurrenceEnd?
    if let endStr = spec.end {
        guard spec.count == nil, let endDate = parseISO(endStr) else { return nil }
        end = EKRecurrenceEnd(end: endDate)
    } else if let count = spec.count {
        guard count > 0 else { return nil }
        end = EKRecurrenceEnd(occurrenceCount: count)
    }

    return EKRecurrenceRule(
        recurrenceWith: freq,
        interval: interval,
        daysOfTheWeek: daysOfWeek,
        daysOfTheMonth: daysOfMonth,
        monthsOfTheYear: spec.monthsOfYear?.map { NSNumber(value: $0) },
        weeksOfTheYear: spec.weeksOfYear?.map { NSNumber(value: $0) },
        daysOfTheYear: spec.daysOfYear?.map { NSNumber(value: $0) },
        setPositions: spec.setPositions?.map { NSNumber(value: $0) },
        end: end
    )
}

// MARK: - Color mapping

func colorForName(_ name: String) -> CGColor? {
    let map: [String: (CGFloat, CGFloat, CGFloat)] = [
        "red":    (1.0, 0.23, 0.19),
        "orange": (1.0, 0.58, 0.0),
        "yellow": (1.0, 0.8, 0.0),
        "green":  (0.3, 0.85, 0.39),
        "blue":   (0.0, 0.48, 1.0),
        "purple": (0.69, 0.32, 0.87),
        "brown":  (0.64, 0.52, 0.37),
        "cyan":   (0.35, 0.78, 0.98),
        "gray":   (91.0 / 255, 98.0 / 255, 106.0 / 255),
        "teal":   (48.0 / 255, 176.0 / 255, 199.0 / 255),
    ]
    guard let (r, g, b) = map[name.lowercased()] else { return nil }
    let space = CGColorSpace(name: CGColorSpace.sRGB)!
    return CGColor(colorSpace: space, components: [r, g, b, 1.0])
}

// MARK: - Helpers

func findReminder(_ store: EKEventStore, id: String) -> EKReminder {
    guard let item = store.calendarItem(withIdentifier: id) as? EKReminder else {
        fail("Reminder not found for id: \(id)")
    }
    return item
}

func normalizedReminderKitListIdentifier(_ raw: String) -> String {
    raw
        .replacingOccurrences(of: "x-apple-reminderkit://REMCDList/", with: "", options: [.caseInsensitive])
        .trimmingCharacters(in: .whitespacesAndNewlines)
}

func isICloudReminderSource(_ source: EKSource?) -> Bool {
    guard let source = source else { return false }
    return source.sourceType == .calDAV && source.title.localizedCaseInsensitiveContains("icloud")
}

func iCloudReminderCalendars(_ store: EKEventStore) -> [EKCalendar] {
    store.calendars(for: .reminder).filter { isICloudReminderSource($0.source) }
}

func findList(_ store: EKEventStore, name: String? = nil, listId: String? = nil) -> EKCalendar {
    let calendars = store.calendars(for: .reminder)
    if let rawListId = listId, !rawListId.isEmpty {
        let wanted = normalizedReminderKitListIdentifier(rawListId)
        let matches = calendars.filter {
            normalizedReminderKitListIdentifier($0.calendarIdentifier).caseInsensitiveCompare(wanted) == .orderedSame
        }
        if matches.count == 1 {
            let cal = matches[0]
            guard isICloudReminderSource(cal.source) else {
                fail("List \(wanted) is not in iCloud Reminders; RemCTL only writes iCloud Reminders lists")
            }
            return cal
        }
        if matches.count > 1 {
            fail("Multiple EventKit reminder lists match id \(wanted)")
        }
        if name == nil || name?.isEmpty == true {
            fail("iCloud Reminders list not found for id: \(wanted)")
        }
        // EventKit and ReminderKit identifiers have diverged across macOS releases.
        // If the stable DB id cannot be matched, fall back to the unique iCloud title below.
    }

    guard let name = name, !name.isEmpty else {
        fail("List name or listId is required")
    }
    let iCloudMatches = iCloudReminderCalendars(store).filter { $0.title == name }
    if iCloudMatches.count == 1 { return iCloudMatches[0] }
    if iCloudMatches.count > 1 {
        fail("Multiple iCloud Reminders lists named \(name); pass --list-id")
    }
    if calendars.contains(where: { $0.title == name }) {
        fail("List \(name) is not in iCloud Reminders; RemCTL does not write non-iCloud Reminders accounts")
    }
    fail("iCloud Reminders list not found: \(name)")
}

func defaultICloudReminderList(_ store: EKEventStore) -> EKCalendar {
    if let cal = store.defaultCalendarForNewReminders(), isICloudReminderSource(cal.source) {
        return cal
    }
    fail("Default Reminders list is not in iCloud; pass -l/--list for an iCloud Reminders list")
}

func applyFields(_ reminder: EKReminder, _ cmd: Command, store: EKEventStore) {
    if let t = cmd.title { reminder.title = t }

    if let list = cmd.list {
        reminder.calendar = findList(store, name: list, listId: cmd.listId)
    } else if let listId = cmd.listId {
        reminder.calendar = findList(store, listId: listId)
    }

    // due: present string → set date, JSON null → clear.
    // Set dueDateComponents WITHOUT .timeZone in the component set AND set
    // reminder.timeZone explicitly — this mirrors Reminders.app's save path and
    // produces a changedKeys set CloudKit accepts as a full dueDate update. The
    // previous variant embedded .timeZone in the components + cleared
    // startDateComponents; that produced a CKRecord push that remindd logged as
    // successful but CloudKit silently ignored the dueDate field
    // (observed 2026-04-17, verified against AppleScript-authored pushes).
    if cmd.due != nil {
        guard let dueStr = cmd.due, !dueStr.isEmpty, let date = parseISO(dueStr) else {
            fail("Invalid due date")
        }
        if cmd.allDay == true {
            reminder.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day], from: date)
        } else {
            reminder.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute, .second], from: date)
        }
        reminder.timeZone = TimeZone.current
    }
    // If due was explicitly null in JSON, the Decodable will still decode it;
    // we handle clearing via the raw JSON check below.

    if let p = cmd.priority {
        guard 0...9 ~= p else { fail("Invalid priority") }
        reminder.priority = p
    }

    // Apply notes first, then URL.
    // EKReminder.url (EventKit) does NOT map to the ZICSURL field that Reminders.app
    // displays — that's a private ReminderKit property. We can read ZICSURL from CoreData
    // but cannot write it via public EventKit APIs. As a fallback, append the URL to the
    // notes field so it appears as a tappable link in the reminder detail view.
    if let n = cmd.notes { reminder.notes = n }
    if let u = cmd.url {
        if let existing = reminder.notes, !existing.isEmpty {
            reminder.notes = existing + "\n\n" + u
        } else {
            reminder.notes = u
        }
    }
    // EventKit has no public flagged API. The old behavior wrote priority=1 as
    // a proxy, which clobbered priority and never set the real ZFLAGGED;
    // refuse instead so callers use AppleScript or remctl-private set_flagged.
    if cmd.flagged != nil {
        fail("flagged is not supported: EventKit cannot set the real flagged state; use the AppleScript path or remctl-private set_flagged")
    }

    if let spec = cmd.recurrence {
        guard let rule = buildRecurrenceRule(spec) else { fail("Invalid recurrence") }
        reminder.recurrenceRules = [rule]
    }

    if cmd.clearAlarms == true {
        reminder.alarms = []
    }

    if let alarmStr = cmd.alarm {
        guard let alarm = parseAlarm(alarmStr) else { fail("Invalid alarm") }
        reminder.alarms = [alarm]
    }

    if cmd.latitude != nil || cmd.longitude != nil {
        guard let latitude = cmd.latitude, let longitude = cmd.longitude else {
            fail("Location alarms require latitude and longitude")
        }
        guard (-90.0...90.0).contains(latitude), (-180.0...180.0).contains(longitude) else {
            fail("Invalid location coordinates")
        }
        let radius = cmd.radius ?? 100.0
        guard radius > 0, radius <= 100_000 else { fail("Invalid location radius") }
        let location = EKStructuredLocation(title: cmd.locationTitle ?? "Location")
        location.geoLocation = CLLocation(latitude: latitude, longitude: longitude)
        location.radius = radius
        let alarm = EKAlarm()
        alarm.structuredLocation = location
        if let proximity = cmd.proximity,
           !["enter", "arriving", "leave", "leaving"].contains(proximity) {
            fail("Invalid location proximity")
        }
        let proximityValue = (cmd.proximity == "leaving" || cmd.proximity == "leave")
            ? EKAlarmProximity.leave
            : EKAlarmProximity.enter
        alarm.proximity = proximityValue
        var alarms = reminder.alarms ?? []
        alarms.removeAll { $0.structuredLocation != nil || $0.proximity != .none }
        alarms.append(alarm)
        reminder.alarms = alarms
    }
}

// MARK: - Main

func requestAccess(_ store: EKEventStore) {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    var accessError: Error?

    store.requestFullAccessToReminders { g, e in
        granted = g
        accessError = e
        sem.signal()
    }
    if sem.wait(timeout: .now() + 30) == .timedOut {
        fail("Timed out waiting for Reminders authorization")
    }

    if let e = accessError { fail("EventKit access error: \(e.localizedDescription)") }
    if !granted { fail("Reminders access not granted") }
}

func authorizationSummary(_ store: EKEventStore) -> [String: Any] {
    [
        "status": "authorized",
        "calendarCount": store.calendars(for: .reminder).count,
        "defaultList": store.defaultCalendarForNewReminders()?.title ?? "",
    ]
}

// MARK: - Limited EventKit reads

let limitedReadWarning = "EventKit read mode is a limited fallback: eventKitId is not a RemCTL numeric id, and private Reminders metadata such as sections, tags, urgent state, rich links, templates, and numeric ids is unavailable."

func priorityName(_ value: Int) -> String {
    if value >= 1 && value <= 4 { return "high" }
    if value == 5 { return "medium" }
    if value >= 6 && value <= 9 { return "low" }
    return "none"
}

func dateFromComponents(_ components: DateComponents?) -> (Date?, Bool) {
    guard var components = components else { return (nil, false) }
    let allDay = components.hour == nil && components.minute == nil && components.second == nil
    if components.calendar == nil {
        components.calendar = Calendar.current
    }
    return (components.date, allDay)
}

func alarmPayload(_ alarm: EKAlarm) -> [String: Any] {
    var payload: [String: Any] = [:]
    if let absoluteDate = alarm.absoluteDate {
        payload["type"] = "absolute"
        payload["date"] = isoFormatter.string(from: absoluteDate)
    } else if alarm.structuredLocation != nil {
        payload["type"] = "location"
        payload["proximity"] = alarm.proximity == .leave ? "leaving" : "arriving"
    } else {
        payload["type"] = "relative"
        payload["relativeOffset"] = alarm.relativeOffset
        payload["relativeOffsetMinutes"] = Int(alarm.relativeOffset / 60.0)
    }
    return payload
}

func recurrencePayload(_ rule: EKRecurrenceRule) -> [String: Any] {
    let frequency: String
    switch rule.frequency {
    case .daily: frequency = "daily"
    case .weekly: frequency = "weekly"
    case .monthly: frequency = "monthly"
    case .yearly: frequency = "yearly"
    @unknown default: frequency = "unknown"
    }
    var payload: [String: Any] = [
        "frequency": frequency,
        "interval": rule.interval,
    ]
    if let days = rule.daysOfTheWeek, !days.isEmpty {
        payload["daysOfWeek"] = days.map { $0.dayOfTheWeek.rawValue }
        let weekNumbers = days.map { $0.weekNumber }
        // Only surfaced when at least one day is pinned to a specific week.
        if weekNumbers.contains(where: { $0 != 0 }) {
            payload["weekNumbers"] = weekNumbers
        }
    }
    if let days = rule.daysOfTheMonth, !days.isEmpty {
        payload["daysOfMonth"] = days.map { $0.intValue }
    }
    return payload
}

func reminderPayload(_ reminder: EKReminder) -> [String: Any] {
    var payload: [String: Any] = [
        "eventKitId": reminder.calendarItemIdentifier,
        "title": reminder.title ?? "",
        "list": reminder.calendar?.title ?? "",
        "completed": reminder.isCompleted,
        "priority": priorityName(Int(reminder.priority)),
    ]
    if let externalId = reminder.calendarItemExternalIdentifier {
        payload["externalId"] = externalId
    }
    if let notes = reminder.notes, !notes.isEmpty {
        payload["notes"] = notes
    }
    if let url = reminder.url {
        payload["url"] = url.absoluteString
    }
    let due = dateFromComponents(reminder.dueDateComponents)
    if let dueDate = due.0 {
        payload["dueDate"] = isoFormatter.string(from: dueDate)
        payload["allDay"] = due.1
    }
    if let creationDate = reminder.creationDate {
        payload["createdDate"] = isoFormatter.string(from: creationDate)
    }
    if let completionDate = reminder.completionDate {
        payload["completionDate"] = isoFormatter.string(from: completionDate)
    }
    if let alarms = reminder.alarms, !alarms.isEmpty {
        payload["alarms"] = alarms.map { alarmPayload($0) }
    }
    if let rules = reminder.recurrenceRules, !rules.isEmpty {
        payload["recurrence"] = recurrencePayload(rules[0])
    }
    return payload
}

func fetchReminders(_ store: EKEventStore, predicate: NSPredicate) -> [EKReminder] {
    let semaphore = DispatchSemaphore(value: 0)
    var result: [EKReminder] = []
    var fetchId: Any?
    fetchId = store.fetchReminders(matching: predicate) { reminders in
        result = reminders ?? []
        semaphore.signal()
    }
    if semaphore.wait(timeout: .now() + 30) == .timedOut {
        if let fetchId = fetchId {
            store.cancelFetchRequest(fetchId)
        }
        fail("EventKit read timed out")
    }
    return result
}

func eventKitCalendars(_ store: EKEventStore, listName: String?) -> [EKCalendar] {
    if let listName = listName, !listName.isEmpty {
        let matches = iCloudReminderCalendars(store).filter { $0.title == listName }
        if matches.isEmpty {
            if store.calendars(for: .reminder).contains(where: { $0.title == listName }) {
                fail("List \(listName) is not in iCloud Reminders; EventKit fallback is iCloud-only")
            }
            fail("iCloud Reminders list not found: \(listName)")
        }
        if matches.count > 1 {
            fail("Multiple iCloud Reminders lists named \(listName); EventKit fallback cannot disambiguate them")
        }
        return matches
    }
    let calendars = iCloudReminderCalendars(store)
    if calendars.isEmpty {
        fail("No iCloud Reminders source found")
    }
    return calendars
}

func containsQuery(_ reminder: EKReminder, _ query: String) -> Bool {
    let options: String.CompareOptions = [.caseInsensitive, .diacriticInsensitive]
    if (reminder.title ?? "").range(of: query, options: options) != nil { return true }
    if (reminder.notes ?? "").range(of: query, options: options) != nil { return true }
    if (reminder.url?.absoluteString ?? "").range(of: query, options: options) != nil { return true }
    return false
}

// MARK: - Geocoding

/// One geocoder match with the fields RemCTL needs to judge its precision.
func placemarkPayload(_ placemark: CLPlacemark) -> [String: Any]? {
    guard let location = placemark.location else { return nil }
    var payload: [String: Any] = [
        "latitude": location.coordinate.latitude,
        "longitude": location.coordinate.longitude,
    ]
    func nonEmpty(_ value: String?) -> String? {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else { return nil }
        return value
    }
    let street = [placemark.subThoroughfare, placemark.thoroughfare].compactMap(nonEmpty).joined(separator: " ")
    let address = [nonEmpty(street), placemark.locality, placemark.administrativeArea, placemark.postalCode, placemark.country]
        .compactMap(nonEmpty)
    if !address.isEmpty { payload["address"] = address.joined(separator: ", ") }
    if let name = nonEmpty(placemark.name) { payload["name"] = name }
    if let thoroughfare = nonEmpty(placemark.thoroughfare) { payload["thoroughfare"] = thoroughfare }
    // Separate parts let RemCTL check the match against the words the user typed.
    let parts: [(String, String?)] = [
        ("subLocality", placemark.subLocality), ("locality", placemark.locality),
        ("subAdministrativeArea", placemark.subAdministrativeArea), ("administrativeArea", placemark.administrativeArea),
        ("postalCode", placemark.postalCode), ("country", placemark.country),
    ]
    for (key, value) in parts {
        if let value = nonEmpty(value) { payload[key] = value }
    }
    if let areas = placemark.areasOfInterest, !areas.isEmpty { payload["areasOfInterest"] = areas }
    if let region = placemark.region as? CLCircularRegion { payload["regionRadius"] = region.radius }
    if let country = nonEmpty(placemark.isoCountryCode) { payload["isoCountryCode"] = country }
    return payload
}

/// Forward-geocode an address with a hard deadline. Needs no Reminders or
/// Location Services permission: it only asks Apple's geocoder, and it never
/// writes anything. On the deadline it cancels the request and reports timeout.
func runGeocode(_ cmd: Command) -> Never {
    guard let address = cmd.address?.trimmingCharacters(in: .whitespacesAndNewlines), !address.isEmpty else {
        fail("address is required for geocode")
    }
    guard address.count <= 512 else { fail("address is longer than 512 characters") }
    let timeout = min(max(cmd.timeoutSeconds ?? 10, 1), 30)
    let geocoder = CLGeocoder()
    var placemarks: [CLPlacemark] = []
    var failure: Error?
    var finished = false
    geocoder.geocodeAddressString(address) { results, error in
        placemarks = results ?? []
        failure = error
        finished = true
    }
    // The completion handler arrives on the main queue, which this loop services.
    let deadline = Date().addingTimeInterval(timeout)
    while !finished && Date() < deadline {
        _ = RunLoop.current.run(mode: .default, before: min(deadline, Date().addingTimeInterval(0.05)))
    }
    if !finished {
        geocoder.cancelGeocode()
        output(["status": "error", "code": "timeout", "message": "Geocoding did not finish within \(Int(timeout)) seconds."])
        exit(1)
    }
    if let error = failure as? CLError {
        switch error.code {
        case .geocodeFoundNoResult, .geocodeFoundPartialResult:
            output(["status": "ok", "query": address, "candidates": [] as [Any]])
            exit(0)
        case .network:
            output(["status": "error", "code": "network", "message": "Apple's geocoder could not be reached: \(error.localizedDescription)"])
        case .denied:
            output(["status": "error", "code": "denied", "message": "Geocoding was denied: \(error.localizedDescription)"])
        case .geocodeCanceled:
            output(["status": "error", "code": "timeout", "message": "Geocoding was cancelled."])
        default:
            output(["status": "error", "code": "failed", "message": "Geocoding failed: \(error.localizedDescription)"])
        }
        exit(1)
    }
    if let error = failure {
        output(["status": "error", "code": "failed", "message": "Geocoding failed: \(error.localizedDescription)"])
        exit(1)
    }
    output(["status": "ok", "query": address, "candidates": placemarks.compactMap(placemarkPayload)])
    exit(0)
}

func runLimitedEventKitRead(_ cmd: Command, store: EKEventStore) {
    guard let mode = cmd.readMode else { fail("readMode is required") }
    let calendars = eventKitCalendars(store, listName: cmd.list)
    let completed = cmd.completed ?? false
    let limit = max(1, min(cmd.limit ?? 500, 1000))
    let now = Date()
    let calendar = Calendar.current
    let startOfToday = calendar.startOfDay(for: now)
    guard let startOfTomorrow = calendar.date(byAdding: .day, value: 1, to: startOfToday) else {
        fail("Invalid date range")
    }
    var reminders: [EKReminder]

    switch mode {
    case "show":
        let predicate = store.predicateForReminders(in: calendars)
        reminders = fetchReminders(store, predicate: predicate)
        if !completed {
            reminders = reminders.filter { !$0.isCompleted }
        }
    case "search":
        guard let query = cmd.query, !query.isEmpty else { fail("query is required") }
        let predicate = store.predicateForReminders(in: calendars)
        reminders = fetchReminders(store, predicate: predicate)
        if !completed {
            reminders = reminders.filter { !$0.isCompleted }
        }
        reminders = reminders.filter { containsQuery($0, query) }
    case "today":
        let includeOverdue = cmd.includeOverdue ?? true
        let start = includeOverdue ? nil : startOfToday
        let predicate = store.predicateForIncompleteReminders(withDueDateStarting: start, ending: startOfTomorrow, calendars: calendars)
        reminders = fetchReminders(store, predicate: predicate)
    case "upcoming":
        let days = max(1, min(cmd.days ?? 7, 3650))
        guard let end = calendar.date(byAdding: .day, value: days + 1, to: startOfToday) else {
            fail("Invalid upcoming range")
        }
        let predicate = store.predicateForIncompleteReminders(withDueDateStarting: startOfToday, ending: end, calendars: calendars)
        reminders = fetchReminders(store, predicate: predicate)
    default:
        fail("Unsupported EventKit read mode: \(mode)")
    }

    // Convert date components once per reminder, rather than on each comparison.
    var sortableReminders = reminders.map {
        (reminder: $0, dueDate: dateFromComponents($0.dueDateComponents).0, title: $0.title ?? "")
    }
    sortableReminders.sort {
        let leftDue = $0.dueDate
        let rightDue = $1.dueDate
        if let leftDue = leftDue, let rightDue = rightDue {
            if leftDue != rightDue { return leftDue < rightDue }
        } else if leftDue != nil {
            return true
        } else if rightDue != nil {
            return false
        }
        return $0.title < $1.title
    }
    let items = sortableReminders.prefix(limit).map { reminderPayload($0.reminder) }
    output([
        "status": "ok",
        "source": "eventkit",
        "fidelity": "limited",
        "mode": mode,
        "idWarning": "eventKitId is not a RemCTL numeric id and cannot be passed to numeric-id commands.",
        "limitations": [
            "No RemCTL numeric ids",
            "No sections",
            "No synced tags",
            "No urgent state",
            "No private rich links",
            "No templates or smart-list internals",
        ],
        "items": Array(items),
        "warning": limitedReadWarning,
    ])
}

// Read stdin. Use readDataToEndOfFile so chunked pipes aren't silently
// truncated (availableData only returns what's buffered at call time).
// Cap at 1 MiB — no legitimate command payload is anywhere near that.
let inputData = FileHandle.standardInput.readDataToEndOfFile()
if inputData.isEmpty { fail("No input on stdin") }
if inputData.count > 1_048_576 { fail("Input too large: \(inputData.count) bytes (max 1 MiB)") }

let cmd: Command
do {
    cmd = try JSONDecoder().decode(Command.self, from: inputData)
} catch {
    fail("Invalid JSON: \(error.localizedDescription)")
}

// Check if "due" was explicitly set to null in the raw JSON
let dueExplicitlyNull: Bool = {
    guard let obj = try? JSONSerialization.jsonObject(with: inputData) as? [String: Any] else { return false }
    return obj.keys.contains("due") && obj["due"] is NSNull
}()

// Geocoding touches no reminders, so it runs before EventKit is opened or asked for access.
if cmd.action == "geocode" {
    runGeocode(cmd)
}

let store = EKEventStore()

if cmd.action == "authorize" {
    requestAccess(store)
    output(authorizationSummary(store))
    exit(0)
}


requestAccess(store)

switch cmd.action {

case "read":
    runLimitedEventKitRead(cmd, store: store)

case "create":
    guard let title = cmd.title, !title.isEmpty else { fail("title is required for create") }
    let reminder = EKReminder(eventStore: store)
    reminder.title = title
    if let list = cmd.list {
        reminder.calendar = findList(store, name: list, listId: cmd.listId)
    } else if let listId = cmd.listId {
        reminder.calendar = findList(store, listId: listId)
    } else {
        reminder.calendar = defaultICloudReminderList(store)
    }
    applyFields(reminder, cmd, store: store)
    do {
        try store.save(reminder, commit: true)
        output(["status": "created", "id": reminder.calendarItemIdentifier, "title": reminder.title ?? ""])
    } catch {
        fail("Save failed: \(error.localizedDescription)")
    }

case "update":
    guard let id = cmd.id else { fail("id is required for update") }
    let reminder = findReminder(store, id: id)
    applyFields(reminder, cmd, store: store)
    if dueExplicitlyNull {
        reminder.dueDateComponents = nil
    }
    do {
        try store.save(reminder, commit: true)
        output(["status": "updated", "id": reminder.calendarItemIdentifier])
    } catch {
        fail("Update failed: \(error.localizedDescription)")
    }

case "delete":
    guard let id = cmd.id else { fail("id is required for delete") }
    let reminder = findReminder(store, id: id)
    do {
        try store.remove(reminder, commit: true)
        output(["status": "deleted", "id": id])
    } catch {
        fail("Delete failed: \(error.localizedDescription)")
    }

case "complete":
    guard let id = cmd.id else { fail("id is required for complete") }
    let reminder = findReminder(store, id: id)
    reminder.isCompleted = true
    if let completionDate = cmd.completionDate {
        guard let date = parseISO(completionDate) else { fail("Invalid completion date") }
        reminder.completionDate = date
    }
    do {
        try store.save(reminder, commit: true)
        var response: [String: Any] = ["status": "completed", "id": id]
        if let completionDate = reminder.completionDate {
            response["completionDate"] = isoFormatter.string(from: completionDate)
        }
        output(response)
    } catch {
        fail("Complete failed: \(error.localizedDescription)")
    }

case "uncomplete":
    guard let id = cmd.id else { fail("id is required for uncomplete") }
    let reminder = findReminder(store, id: id)
    reminder.isCompleted = false
    reminder.completionDate = nil
    do {
        try store.save(reminder, commit: true)
        output(["status": "uncompleted", "id": id])
    } catch {
        fail("Uncomplete failed: \(error.localizedDescription)")
    }

case "flag", "unflag":
    // EventKit has no public flagged API. The old priority=1 proxy reported
    // success without ever touching ZFLAGGED and clobbered priority on the
    // way (unflag reset a genuine High priority to none); refuse instead so
    // callers use AppleScript or remctl-private set_flagged.
    fail("EventKit cannot set the real flagged state; use the AppleScript path or remctl-private set_flagged")

case "create_list":
    guard let title = cmd.title, !title.isEmpty else { fail("title is required for create_list") }
    let cal = EKCalendar(for: .reminder, eventStore: store)
    cal.title = title
    // Use the local/iCloud source for reminders
    guard let source = store.sources.first(where: { isICloudReminderSource($0) }) else {
        fail("No iCloud Reminders source found")
    }
    cal.source = source
    if let colorName = cmd.color {
        guard let cg = colorForName(colorName) else { fail("Unsupported list color: \(colorName)") }
        cal.cgColor = cg
    }
    do {
        try store.saveCalendar(cal, commit: true)
        output(["status": "created", "id": cal.calendarIdentifier, "title": cal.title])
    } catch {
        fail("Create list failed: \(error.localizedDescription)")
    }

case "rename_list":
    guard let title = cmd.title else { fail("title is required for rename_list") }
    guard let newTitle = cmd.newTitle else { fail("newTitle is required for rename_list") }
    let cal = findList(store, name: title, listId: cmd.listId)
    cal.title = newTitle
    do {
        try store.saveCalendar(cal, commit: true)
        output(["status": "renamed", "id": cal.calendarIdentifier, "title": cal.title])
    } catch {
        fail("Rename list failed: \(error.localizedDescription)")
    }

case "delete_list":
    guard let title = cmd.title else { fail("title is required for delete_list") }
    let cal = findList(store, name: title, listId: cmd.listId)
    do {
        try store.removeCalendar(cal, commit: true)
        output(["status": "deleted", "title": title])
    } catch {
        fail("Delete list failed: \(error.localizedDescription)")
    }

default:
    fail("Unknown action: \(cmd.action)")
}

exit(0)
