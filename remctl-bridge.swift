import Foundation
import EventKit

// MARK: - JSON Models

struct Command: Decodable {
    let action: String
    let id: String?
    let title: String?
    let newTitle: String?
    let list: String?
    let due: String?
    let priority: Int?
    let notes: String?
    let url: String?
    let flagged: Bool?
    let recurrence: RecurrenceSpec?
    let alarm: String?
    let color: String?
}

struct RecurrenceSpec: Decodable {
    let frequency: String
    let interval: Int?
    let daysOfWeek: [Int]?
    let daysOfMonth: [Int]?
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

func parseISO(_ s: String) -> Date? {
    isoFormatter.date(from: s) ?? isoDateOnly.date(from: s)
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

    var daysOfWeek: [EKRecurrenceDayOfWeek]?
    if let days = spec.daysOfWeek {
        // Input: 1=Sun, 2=Mon ... 7=Sat → EKWeekday raw values match
        daysOfWeek = days.compactMap { EKWeekday(rawValue: $0) }.map { EKRecurrenceDayOfWeek($0) }
    }

    var daysOfMonth: [NSNumber]?
    if let days = spec.daysOfMonth {
        daysOfMonth = days.map { NSNumber(value: $0) }
    }

    var end: EKRecurrenceEnd?
    if let endStr = spec.end, let endDate = parseISO(endStr) {
        end = EKRecurrenceEnd(end: endDate)
    }

    return EKRecurrenceRule(
        recurrenceWith: freq,
        interval: interval,
        daysOfTheWeek: daysOfWeek,
        daysOfTheMonth: daysOfMonth,
        monthsOfTheYear: nil,
        weeksOfTheYear: nil,
        daysOfTheYear: nil,
        setPositions: nil,
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
    ]
    guard let (r, g, b) = map[name.lowercased()] else { return nil }
    return CGColor(red: r, green: g, blue: b, alpha: 1.0)
}

// MARK: - Helpers

func findReminder(_ store: EKEventStore, id: String) -> EKReminder {
    guard let item = store.calendarItem(withIdentifier: id) as? EKReminder else {
        fail("Reminder not found for id: \(id)")
    }
    return item
}

func findList(_ store: EKEventStore, name: String) -> EKCalendar {
    guard let cal = store.calendars(for: .reminder).first(where: { $0.title == name }) else {
        fail("List not found: \(name)")
    }
    return cal
}

func applyFields(_ reminder: EKReminder, _ cmd: Command, store: EKEventStore) {
    if let t = cmd.title { reminder.title = t }

    if let list = cmd.list {
        reminder.calendar = findList(store, name: list)
    }

    // due: present string → set date, JSON null → clear
    if cmd.due != nil {
        if let dueStr = cmd.due, !dueStr.isEmpty, let date = parseISO(dueStr) {
            reminder.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute, .second, .timeZone], from: date)
        }
    }
    // If due was explicitly null in JSON, the Decodable will still decode it;
    // we handle clearing via the raw JSON check below.

    if let p = cmd.priority { reminder.priority = p }
    if let n = cmd.notes { reminder.notes = n }
    if let u = cmd.url, let url = URL(string: u) { reminder.url = url }
    // EventKit has no public flagged API; use priority 1 as a proxy (shows flag in Reminders.app)
    if let f = cmd.flagged {
        if f && reminder.priority == 0 { reminder.priority = 1 }
        else if !f && reminder.priority == 1 { reminder.priority = 0 }
    }

    if let spec = cmd.recurrence, let rule = buildRecurrenceRule(spec) {
        reminder.recurrenceRules = [rule]
    }

    if let alarmStr = cmd.alarm, let alarm = parseAlarm(alarmStr) {
        reminder.alarms = [alarm]
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
    sem.wait()

    if let e = accessError { fail("EventKit access error: \(e.localizedDescription)") }
    if !granted { fail("Reminders access not granted") }
}

// Read stdin
guard let inputData = FileHandle.standardInput.availableData as Data?,
      !inputData.isEmpty else {
    fail("No input on stdin")
}

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

let store = EKEventStore()
requestAccess(store)

switch cmd.action {

case "create":
    guard let title = cmd.title, !title.isEmpty else { fail("title is required for create") }
    let reminder = EKReminder(eventStore: store)
    reminder.title = title
    if let list = cmd.list {
        reminder.calendar = findList(store, name: list)
    } else {
        reminder.calendar = store.defaultCalendarForNewReminders()
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
    do {
        try store.save(reminder, commit: true)
        output(["status": "completed", "id": id])
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

case "flag":
    // EventKit has no public flagged API; set priority=1 as a proxy (shows flag in Reminders.app)
    guard let id = cmd.id else { fail("id is required for flag") }
    let reminder = findReminder(store, id: id)
    if reminder.priority == 0 { reminder.priority = 1 }
    do {
        try store.save(reminder, commit: true)
        output(["status": "flagged", "id": id])
    } catch {
        fail("Flag failed: \(error.localizedDescription)")
    }

case "unflag":
    guard let id = cmd.id else { fail("id is required for unflag") }
    let reminder = findReminder(store, id: id)
    if reminder.priority == 1 { reminder.priority = 0 }
    do {
        try store.save(reminder, commit: true)
        output(["status": "unflagged", "id": id])
    } catch {
        fail("Unflag failed: \(error.localizedDescription)")
    }

case "create_list":
    guard let title = cmd.title, !title.isEmpty else { fail("title is required for create_list") }
    let cal = EKCalendar(for: .reminder, eventStore: store)
    cal.title = title
    // Use the local/iCloud source for reminders
    guard let source = store.sources.first(where: { $0.sourceType == .calDAV })
            ?? store.sources.first(where: { $0.sourceType == .local }) else {
        fail("No suitable calendar source found")
    }
    cal.source = source
    if let colorName = cmd.color, let cg = colorForName(colorName) {
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
    let cal = findList(store, name: title)
    cal.title = newTitle
    do {
        try store.saveCalendar(cal, commit: true)
        output(["status": "renamed", "id": cal.calendarIdentifier, "title": cal.title])
    } catch {
        fail("Rename list failed: \(error.localizedDescription)")
    }

case "delete_list":
    guard let title = cmd.title else { fail("title is required for delete_list") }
    let cal = findList(store, name: title)
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
