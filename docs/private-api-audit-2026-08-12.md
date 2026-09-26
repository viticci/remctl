# RemCTL private ReminderKit audit

Historical test record. Versions, counts, and results below describe the dated runs, not the current installation.

Date: 2026-08-12

## Decision

RemCTL should adopt MachOSwiftSection as a manual compatibility and drift-audit tool. It should not link `ReminderKitInternal` or rewrite the working Objective-C helper around recovered Swift declarations.

`remctl-private` is the Objective-C helper that RemCTL launches for metadata EventKit cannot write: tags, sections, subtasks, smart lists, templates, groceries, rich URLs, assignments, Early Reminders, and related list properties. It links only `ReminderKit`, Foundation, and AppKit. The audited v1.6.1 and compatibility follow-up used helper protocol 1; the later reminder-ordering follow-up below raises the helper protocol to 2.

The audit found two incompatibilities on the tested host:

1. Grocery fallback categorization calls a removed selector.
2. Built-in smart-list pinning depends on a fetch selector that no longer exists.

Grocery items normally sort automatically, so the broken fallback runs only when that sorting fails.

The original audit changed no Reminders data and did not install, commit, push, or modify product code. The cross-version follow-up below is a separate implementation and validation pass.

## Cross-version follow-up

The compatibility target is Golden Gate `27.0` build `26A5406e` on the Mac Studio and the previous Tahoe release `26.2` build `25C56` on an M4 Mac mini. The Mini has Apple Command Line Tools `26.6`, Swift `6.3.3`, clang `21.0.0`, system Python `3.9.6`, and an existing `uv` Python `3.12.13`; full Xcode and Homebrew are not required. MachOSwiftSection `0.15.1` was built natively for arm64 from the pinned source checkout and installed at `~/.local/bin/swift-section` on the Mini.

The implementation deliberately keeps the Objective-C helper and its protocol-1 compatibility:

1. `REMColor.colorSpace` now uses the runtime-correct unsigned declaration.
2. Grocery fallback dispatch supports two receiver/argument contracts. Tahoe retains `categorizeGroceryItemsWithReminderIDs:` on the grocery-context change with UUID values. Golden Gate calls `autoCategorizeRemindersWithReminderIDs:` on the list change with `REMObjectID` values. A selector-name-only Golden Gate substitution failed. A Tahoe `REMObjectID` experiment was rejected because it tombstoned the disposable objects.
3. The Python CLI tells the helper whether a smart-list target is custom. Custom targets use only the custom fetch. Built-in targets use the generic fetch only when that host exposes it; otherwise they fail with a precise unsupported message before a save request.
4. A read-only `capabilities` action reports the watched selector set and normalized encodings from real objects, plus `saveCalled: false`.
5. Dead private metadata code was removed. The EventKit-first location-alarm fallback stays because it still protects hosts without the bridge; removing it would be a regression with no compatibility benefit.

This is not a `ReminderKitInternal` adoption. SQLite remains the rich read source, and `swift-section` remains a maintainer audit dependency only.

The generic smart-list declaration remains as a capability check and compatibility path for hosts that still expose it. Removing it would unnecessarily break those hosts. Tested Tahoe 26.2 and Golden Gate 27.0 builds both lack it, so neither attempts a built-in smart-list save.

The follow-up helper declares **34 Objective-C classes, 111 methods, and no protocols**. It contains 74 `respondsToSelector:` checks covering 40 unique literal selectors. The added declaration is `autoCategorizeRemindersWithReminderIDs:` on `REMListChangeItem`. The original v1.6.1 counts and source links below describe the audited baseline; links are pinned to commit `90ebd57` so later source edits do not move their evidence.

### Cross-version results

Golden Gate:

- The read-only probe found the legacy grocery selector absent, the new list-change selector present as `v24@0:8@16`, the generic smart-list fetch absent, and the custom fetch present as `@32@0:8@16^@24`. It returned `saveCalled: false`.
- Passing UUIDs to the renamed selector failed with `Couldn’t communicate with a helper application.` Calling it on the list change with `REMObjectID` values passed, saved, and placed the disposable “Bananas Direct” reminder in the Produce section.
- All 368 unit tests passed. The full disposable private matrix passed, including the direct renamed-selector write, custom-smart-list pin/unpin, precise built-in pin rejection, rich reminder metadata, templates, and cleanup.

Tahoe:

- Runtime metadata found the legacy grocery selector present as `v24@0:8@16`, the Golden Gate selector absent, the generic smart-list fetch absent, the custom fetch present, and the corrected `REMColor.colorSpace` encoding `Q`.
- All 368 unit tests passed under Python 3.12.13. All three helpers built with the Mini's Command Line Tools.
- The shipped UUID fallback returned a helper-communication error in the scoped Script Editor context. A temporary `REMObjectID` variant returned success and created a Household Items membership, but it also set `ZMARKEDFORDELETION=1` on the disposable list, reminder, and section. That destructive alternative was rejected; the product keeps Tahoe's existing UUID path. Product read semantics confirmed zero active prefix rows afterward.
- Custom smart-list pin/unpin passed in both protocol-1 compatibility directions: an old payload without `isCustom` against the new helper, and a current payload with `isCustom: true` against the v1.6.1 helper. The built-in rejection left its row unchanged, and cleanup removed the disposable custom list.

The CLI/helper protocol stays at version 1 because the payload change is additive. A new CLI sends `isCustom`, which an old helper ignores. A new helper retains the old fetch order when `isCustom` is absent. Ordinary public writes, reads, and unrelated private commands do not change. The only user-visible smart-list change is that an already-broken built-in pin attempt now fails earlier and precisely; custom pinning remains supported.

## 2026-08-13 reminder-ordering follow-up

The ordering review evaluated pull request 26 independently from its pull request 25 dependency. The broad smart-list-section rewrite in pull request 25 was rejected because it broke existing schemas, commands, and 19 unit tests. The retained implementation adds one dedicated read query for custom-smart-list section detection and leaves the ordinary section path unchanged.

`reminder-move --private` adds `--before`, `--after`, `--first`, and `--last` ordering for ordinary lists and unsectioned custom smart lists. It increments the helper protocol to 2 because protocol-1 helpers do not understand either ordering action. Existing commands and payloads are otherwise unchanged; a stale helper fails the existing protocol preflight before any write.

Runtime metadata on both Golden Gate 27.0 and Tahoe 26.2 reports:

- `REMListChangeItem.insertReminderChangeItem:beforeReminderChangeItem:` and `insertReminderChangeItem:afterReminderChangeItem:` as `v32@0:8@16@24`.
- `REMList.reminderIDsOrdering` as `@16@0:8`.
- `REMSmartListChangeItem.updateManualOrdering:` as `v24@0:8@16`.
- `REMManualOrdering.initWithObjectID:listType:listID:topLevelElementIDs:secondaryLevelElementIDsByTopLevelElementID:uncommitedElementsAccountID:modifiedDate:` as `@68@0:8@16s24@28@36@44@52@60`.
- The pull request's attempted `setSortingStyle:` selector is absent.

The final helper uses `REMObjectID` values for the manual ordering's top-level array and secondary-level dictionary keys. The pull request used string keys, which did not match the recovered `[REMObjectID: [REMObjectID]]` contract. Sectioned custom smart lists are refused before saving because their secondary-level ordering shape has not received a disposable write/readback test.

Golden Gate passed 378 unit tests, a strict helper build, the full disposable private matrix with an ordinary-list reorder, and a separate disposable unsectioned custom-smart-list test. That second test created a manual-order record through ReminderKit, reordered it through the CLI, read back `verified: true`, and removed the smart list, base list, and reminders; all prefix counts were zero after cleanup.

Tahoe passed the same 378 unit tests and strict helper build. Its read-only protocol-2 capability probe reported every watched ordering selector available and `saveCalled: false`. A Tahoe live ordering write was not run: the SSH host context does not have EventKit/Reminders authorization, and the prior temporary Script Editor permission had been revoked. This is a recorded validation limit, not a claimed live pass.

## Original audit baseline

Original audit environment:

- RemCTL: `1.6.1`
- macOS: `27.0`, build `26A5406e`
- Architecture: `arm64`
- Xcode beta Swift: `6.4`
- MachOSwiftSection: `0.15.1`
- Repository-local `AGENTS.md`: absent
- Unit tests: **368 passed** in 3.444 seconds
- Python compilation: passed
- `remctl-bridge.swift`: built in a temporary directory
- `remctl-permissions.swift`: built in a temporary directory
- `remctl-private.m`: built in a temporary directory
- `remctl doctor --for-agent --json`: 0 failures; one zsh completion-path warning; private helper protocol 1
- `git diff --check`: passed
- Live mutation matrices were intentionally not run.

The older repository compatibility report records a successful private write matrix on macOS 27 build `26A5353q`, including groceries, smart lists, templates, subtasks, sections, tags, images, urgent state, flags, and Early Reminders. That remains useful historical evidence, but it is not proof for current build `26A5406e`. See [macos27-compat-review.md](macos27-compat-review.md#write-compatibility).

Git started and ended as:

```text
## main...origin/main
?? .papercuts.jsonl
?? BUG_REPORT_flagging.md
```

`BUG_REPORT_flagging.md` was pre-existing. `.papercuts.jsonl` was created by the repository’s required friction logging. There are no tracked changes from the audit.

## Original v1.6.1 dependency inventory

The audited v1.6.1 helper manually declared **34 Objective-C classes, 110 methods, and no protocols** in [remctl-private.m](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L8):

- Core: `REMObjectID`, `REMStore`, `REMSaveRequest` — lines 8–44
- Accounts: `REMAccount`, `REMAccountChangeItem`, `REMAccountCapabilities` — lines 46–59
- Smart lists: `REMSmartListChangeItem`, `REMSmartListCustomContextChangeItem`, `REMSmartList` — lines 61–83
- Templates: `REMTemplate`, `REMTemplateChangeItem`, `REMTemplateConfiguration` — lines 85–96
- Reminders and metadata contexts: nine classes — lines 98–146
- Lists, groceries, appearance, badges, colors: six classes — lines 148–186
- Sections and memberships: four classes — lines 188–206
- Private location alarms: three classes — lines 208–218

The exact declaration inventory is:

| Class | Declaration and manually declared selectors |
|---|---|
| `REMObjectID` | L8; `objectIDWithURL:` L9, `uuid` L10, `urlRepresentation` L11 |
| `REMStore` | L14; `fetchReminderWithObjectID:error:` L15, `fetchListWithObjectID:error:` L16, `fetchSmartListWithObjectID:error:` L17, `fetchListSectionWithObjectID:error:` L18, `fetchCustomSmartListWithObjectID:error:` L19, `fetchTemplateWithObjectID:error:` L20, `fetchPrimaryActiveCloudKitAccountWithError:` L21, `fetchDefaultAccountWithError:` L22 |
| `REMSaveRequest` | L25; `initWithStore:` L26, `updateAccount:` L27, `updateReminder:` L28, `updateList:` L29, `updateListSection:` L30, `updateSmartList:` L31, `updateTemplate:` L32, `addReminderWithTitle:toReminderSubtaskContextChangeItem:` L33, `_copyReminder:toListChangeItem:` L34, `_copyReminder:toReminderSubtaskContextChangeItem:` L35, `addListWithName:toAccountChangeItem:listObjectID:` L36, `addGroupWithName:toAccountGroupContextChangeItem:` L37, `addGroupWithName:toAccountGroupContextChangeItem:groupObjectID:` L38, `addListSectionWithDisplayName:toListSectionContextChangeItem:` L39, `addCustomSmartListWithName:toAccountChangeItem:smartListObjectID:` L40, `addTemplateWithName:configuration:toAccountChangeItem:` L41, `addListUsingTemplate:toAccountChangeItem:` L42, `saveSynchronouslyWithError:` L43 |
| `REMAccount` | L46; `capabilities` L47, `remObjectID` L48 |
| `REMAccountChangeItem` | L51; `addListChangeItem:` L52, `addSmartListChangeItem:` L53, `groupContext` L54 |
| `REMAccountCapabilities` | L57; `supportsCustomSmartLists` L58 |
| `REMSmartListChangeItem` | L61; `remObjectID` L62, `appearanceContext` L63, `customContext` L64, `setColor:` L65, `setFilterData:` L66, `setIsPinned:` L67, `setName:` L68, `setParentOwnerID:` L69, `setSmartListType:` L70, `removeFromParentWithAccountChangeItem:` L71 |
| `REMSmartListCustomContextChangeItem` | L74; `setName:` L75, `setColor:` L76, `setBadge:` L77 |
| `REMSmartList` | L80; `account` L81, `remObjectID` L82 |
| `REMTemplate` | L85; `remObjectID` L86 |
| `REMTemplateChangeItem` | L89; `remObjectID` L90, `removeFromParentAccount` L91 |
| `REMTemplateConfiguration` | L94; `initWithSourceListID:shouldSaveCompleted:` L95 |
| `REMReminderChangeItem` | L98; `remObjectID` L99, `assignmentContext` L100, `attachmentContext` L101, `dueDateDeltaAlertContext` L102, `flaggedContext` L103, `hashtagContext` L104, `subtaskContext` L105, `urgentAlarmContext` L106, `addAlarm:` L107 |
| `REMReminderAssignmentContextChangeItem` | L110; `addAssignmentWithAssigneeID:originatorID:status:` L111, `removeAllAssignments` L112 |
| `REMReminderDueDateDeltaAlertContextChangeItem` | L115; `addDueDateDeltaAlertWithDueDateDelta:` L116, `removeAllFetchedDueDateDeltaAlerts` L117, `removeDueDateDeltaAlertsWithIdentifiers:` L118 |
| `REMDueDateDeltaInterval` | L121; `initWithUnit:count:` L122 |
| `REMReminderAttachmentContextChangeItem` | L125; `addImageAttachmentWithURL:width:height:error:` L126, `addURLAttachmentWithURL:` L127 |
| `REMReminderHashtagContextChangeItem` | L130; `addHashtagWithType:name:` L131, `removeAllHashtags` L132 |
| `REMReminderFlaggedContextChangeItem` | L135; `setFlagged:` L136 |
| `REMReminderUrgentAlarmContextChangeItem` | L139; `setIsUrgentStateEnabledForCurrentUser:` L140 |
| `REMReminder` | L143; `list` L144, `remObjectID` L145 |
| `REMListChangeItem` | L148; `remObjectID` L149, `sublistContext` L150, `sectionsContextChangeItem` L151, `appearanceContext` L152, `groceryContextChangeItem` L153, `setColor:` L154, `setIsPinned:` L155, `setName:` L156, `setParentOwnerID:` L157, `setParentSubContainerID:` L158, `removeFromParentWithAccountChangeItem:` L159 |
| `REMList` | L162; `account` L163, `remObjectID` L164, `parentList` L165 |
| `REMListGroceryContextChangeItem` | L168; `setShouldCategorizeGroceryItems:` L169, `setGroceryLocaleID:` L170, `categorizeGroceryItemsWithReminderIDs:` L171 |
| `REMListAppearanceContextChangeItem` | L174; `setBadgeEmblem:` L175, `setBadge:` L176 |
| `REMListBadge` | L179; `initWithEmblem:` L180, `initWithEmoji:` L181 |
| `REMColor` | L184; `initWithRed:green:blue:alpha:colorSpace:daSymbolicColorName:daHexString:ckSymbolicColorName:` L185 |
| `REMListSectionChangeItem` | L188; `remObjectID` L189, `setDisplayName:` L190, `removeFromList` L191 |
| `REMListSectionContextChangeItem` | L194; `setShouldUpdateSectionsOrdering:` L195, `setUnsavedMembershipsOfRemindersInSections:` L196, `setUnsavedSectionIDsOrdering:` L197 |
| `REMMembership` | L200; `initWithMemberIdentifier:groupIdentifier:isObsolete:modifiedOn:` L201 |
| `REMMemberships` | L204; `initWithMemberships:` L205 |
| `REMStructuredLocation` | L208; `initWithTitle:locationUID:latitude:longitude:radius:address:routing:referenceFrameString:contactLabel:mapKitHandle:` L209 |
| `REMAlarmLocationTrigger` | L212; `initWithStructuredLocation:proximity:` L213 |
| `REMAlarm` | L216; `initWithTrigger:` L217 |

There are:

- 68 `respondsToSelector:` checks covering 38 unique selectors.
- No `NSClassFromString`.
- No `NSSelectorFromString`.
- No `objc_msgSend`, `dlopen`, or `dlsym` in product code.
- Two KVC property writes for custom-smart-list versions at [remctl-private.m:269](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L269).
- Six manually constructed object-ID URL forms for reminders, sections, sharees, lists, smart lists, and templates at [remctl-private.m:395](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L395).
- A top-level Objective-C exception boundary at [remctl-private.m:732](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L732), plus focused catches for KVC, groceries, and subtask creation.
- Python-side timeout, protocol, retry, and partial-success handling beginning at [remctl:3693](https://github.com/viticci/remctl/blob/90ebd57/remctl#L3693). Additive operations are not retried blindly.

## Original v1.6.1 API-to-evidence map

| Area | Current dependencies and source | Commands and tests | Evidence | Finding |
|---|---|---|---|---|
| Object IDs, store, saves | `REMObjectID`, `REMStore`, `REMSaveRequest`; declarations lines 8–44; URL construction lines 395–434 | All private commands; helper/protocol tests [test_cli.py:390](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L390) | **Both** | All core classes exist. A live list ID round-tripped successfully. Swift metadata adds typed async/throwing store and save wrappers. |
| Lists and groups | Account/list change objects, group contexts; actions [remctl-private.m:775](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L775) | `list-create`, grouping, list delete/move; tests [test_cli.py:959](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L959) | **Objective-C** | Current selectors and scalar encodings are present. |
| Sections | Section change/context and membership classes; actions [remctl-private.m:988](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L988) and [remctl-private.m:1724](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1724) | Section CRUD and assignment; tests [test_cli.py:7426](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L7426) | **Both** | Actual section change objects forward `remObjectID` and `setDisplayName:` successfully. Swift metadata exposes typed section fetches and membership/order protocols. |
| Smart lists | Lines 61–83; create/edit/delete [remctl-private.m:1081](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1081); pin [remctl-private.m:1461](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1461) | Smart-list commands; tests [test_cli.py:2542](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L2542) | **Both plus assumptions** | Custom change setters forward correctly. Generic smart-list fetch is absent; built-in pinning is broken. Swift metadata exposes `REMCustomSmartListFilterDescriptor` and a separate non-custom fetch API. |
| Templates | Lines 85–96; actions [remctl-private.m:1245](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1245) | Template commands; tests [test_cli.py:1496](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L1496) | **Objective-C; partial Swift** | Template context and `fetchTemplatesWithError:` are present. No tested replacement was found. |
| Appearance | Lines 174–186; actions [remctl-private.m:1378](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1378) | List/smart-list appearance; tests [test_cli.py:1856](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L1856) | **Objective-C** | Color, badge, and pin selectors exist. One `REMColor` parameter is declared with the wrong signedness. |
| Groceries | Lines 168–172; metadata helper [remctl-private.m:340](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L340); explicit categorizer [remctl-private.m:1508](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1508) | `--grocery`; orchestration [remctl:4972](https://github.com/viticci/remctl/blob/90ebd57/remctl#L4972); tests [test_cli.py:2176](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L2176) | **Both plus obsolete assumption** | Conversion and locale setters exist. Explicit fallback selector changed. Swift metadata also shows substantial grocery API changes. |
| Tags and rich URLs | Hashtag and attachment contexts, lines 125–133; dispatch [remctl-private.m:1678](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1678) | `--tags`, `--url`, replacement/removal tests [test_cli.py:7301](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L7301) | **Objective-C** | Existing APIs are present. No better Swift path justified another dependency. |
| Images | `addImageAttachmentWithURL:width:height:error:` line 126; use [remctl-private.m:1796](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1796) | `--image` and private-add tests | **Objective-C** | Signature and unsigned dimensions match runtime encoding. |
| Subtasks and clone | `REMSaveRequest` additions/copy methods lines 33–35; use [remctl-private.m:1586](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1586) and [remctl-private.m:1693](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1693) | Subtask tests [test_cli.py:3394](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L3394) | **Both** | Current Objective-C methods exist. Swift metadata added a subtask context type, but it does not remove the need for exception handling. |
| Assignments | Lines 110–113; use [remctl-private.m:1768](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1768) | `--assign`; tests [test_cli.py:4189](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L4189) | **Objective-C** | Assignment creation and clear selectors exist with matching encodings. |
| Flag, urgent, Early Reminder | Lines 115–141; use [remctl-private.m:1821](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1821) | Private edit paths; tests [test_cli.py:3919](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L3919) | **Objective-C** | Current contracts match. Keep database readback fallbacks and exception handling. |
| Location alarms | Lines 208–218; use [remctl-private.m:1864](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1864) | Normal CLI prefers EventKit at [remctl:4567](https://github.com/viticci/remctl/blob/90ebd57/remctl#L4567); tests [test_cli.py:504](https://github.com/viticci/remctl/blob/90ebd57/tests/test_cli.py#L504) | **Objective-C** | Private implementation is a fallback. EventKit should remain primary. |

## Original v1.6.1 incompatibilities and suspicious declarations

### 1. Grocery categorization selector drift

RemCTL declares and checks:

```objc
categorizeGroceryItemsWithReminderIDs:
```

at [remctl-private.m:171](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L171) and calls it at [remctl-private.m:1550](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1550).

A live, unsaved `REMListGroceryContextChangeItem` on macOS build `26A5406e` reports:

- `categorizeGroceryItemsWithReminderIDs:` — absent
- `autoCategorizeRemindersWithReminderIDs:` — present, encoding `v24@0:8@16`

This guarantees the explicit private fallback returns “does not support item categorization” on this host.

Impact is limited because RemCTL waits for Reminders’ automatic categorization before invoking the helper. If the automatic path succeeds, the helper is never called. If it does not, the fallback cannot currently recover.

### 2. Built-in smart-list pin fetch is invalid

RemCTL declares `fetchSmartListWithObjectID:error:` at [remctl-private.m:17](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L17) and conditionally calls it at [remctl-private.m:1478](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L1478).

Current evidence:

- `REMStore` does not respond to that selector.
- The subsequent `fetchCustomSmartListWithObjectID:error:` fallback rejects a real built-in smart-list ID with `com.apple.reminderkit` error `-3000`.
- No save was attempted.

Therefore built-in smart-list pin/unpin cannot work through this helper on the current host. Custom smart-list fetch and change paths remain available.

Swift metadata exposes `fetchNonCustomSmartList(withType:)`, but that takes a private `NonCustom` type rather than an object ID. It is not a drop-in replacement.

### 3. `REMColor` signedness mismatch

[remctl-private.m:185](https://github.com/viticci/remctl/blob/90ebd57/remctl-private.m#L185) declares `colorSpace:` as `NSInteger`, while the runtime encoding uses `Q`, meaning `NSUInteger`.

Both use one 64-bit register on arm64, so this is unlikely to break current calls using value `2`. It is still an incorrect declaration and should be corrected.

### 4. Forwarded methods look absent in class dumps

Of 110 declarations:

- 95 appear directly in the class method lists.
- 15 do not.

Live unsaved objects proved that 11 of those 15 are handled through dynamic forwarding, including `remObjectID`, `setName:`, `setColor:`, `setFilterData:`, and `setDisplayName:`. Smart-list `appearanceContext` correctly returns false and RemCTL falls back to `customContext`. Template-change `remObjectID` was not runtime-tested because no write or disposable template was authorized.

This is why raw class-method dumps alone would have produced false positives.

### 5. Smart-list version KVC remains opaque

The helper sets `minimumSupportedVersion` and `effectiveMinimumSupportedVersion` to `20220430` using KVC. Exceptions are caught, but there is no readback or capability probe. Swift metadata confirms versioned smart-list serialization exists; it does not prove these two KVC keys still control a change item.

### 6. Dead compatibility code — resolved

The original audit found `applyPrivateMetadataToChange` unused and `-Wall -Wextra` reported it plus unused `argc` and `argv`. The follow-up removed that function, its four now-unreferenced helper functions, and the unused parameters. The current helper builds cleanly with `-Wall -Wextra -Werror`.

## Framework and tool evidence

| Target | Location/source | Version and UUID | MachOSwiftSection result |
|---|---|---|---|
| Host ReminderKit | System dyld cache; logical path `/System/Library/PrivateFrameworks/ReminderKit.framework/ReminderKit` | `4046.21`, arm64e, UUID `3C0B8A47-1610-36AC-BAD9-13C1A6CEEF59` | 0 Swift types/protocols; 0.05s. This is an Objective-C framework, not an absent framework. |
| Host ReminderKitInternal | System dyld cache; corresponding private path | `4046.21`, arm64e, UUID `AED1891A-C4B4-3871-882C-29BF6640F9F8` | 934 types, 44 protocols, 2,636 conformances, 10,202 symbols; 28 C-imported types skipped; 5.44s. |
| iOS Simulator 27.0 | Runtime build `24A5390f` | ReminderKit `4043`; bundle has no loose executable and the runtime has no usable cache image | ReminderKit itself unverifiable with this tool/runtime layout. |
| iOS Simulator ReminderKitInternal | Loose arm64 binary in the 27.0 RuntimeRoot | `4043`, UUID `AD4A5FC5-261C-37F6-8C29-1A188D47A8BD` | 947 types, 47 protocols, 2,705 conformances, 10,328 symbols; 27 C-imported types skipped; 7.17s without layouts. |
| iOS Simulator 26.4 comparison | Runtime build `23E254a` | Internal `3973.81`, UUID `741839AF-19ED-309B-A6C8-7C4AB0E9A9CB` | ABI snapshot took 4.96s. |

`remctl-private` directly links no additional private framework. In particular, it does **not** link `ReminderKitInternal`.

The iOS 26.4→27.0 Swift snapshot diff is ABI-breaking according to MachOSwiftSection. It contains 80 added and 37 modified containers, plus 64 added, 41 modified, and 37 removed members. Relevant changes include:

- New typed async/throwing store fetches for reminders, lists, sections, and custom smart lists.
- New `REMSaveRequest.save(executor:) async throws`.
- `fetchList_Sections` gained `fetchPredefinedGroceryListSections`.
- Suggested grocery section names changed to canonical-name contracts.
- `REMGroceryCategory` and related availability types appeared.

Raw evidence was kept outside the repository in `/tmp/remctl-private-api-audit.UokCTM`:

- `diff-summary.json`
- `layout-quality.json`
- `read-probe-macOS-26A5406e.json`
- `remctl-private-manual-declarations.json`
- `manual-vs-objc.json`
- Complete host and Simulator interfaces and ABI snapshots

These paths are temporary and are not intended as durable repository artifacts.

## Recovered Swift contracts

Recovered declarations included:

- `REMStore.fetchReminder(...) async throws`
- `REMStore.fetchReminders(...) async throws`
- `REMStore.fetchListSections(...) async throws`
- `REMStore.fetchCustomSmartList(...) async throws`
- `REMStore.fetchNonCustomSmartList(withType:) throws`
- `REMStore.fetchList(...) async throws`
- `REMSaveRequest.save(executor:) async throws`
- Generic `invoke<A,B>` constrained through `REMStoreInvocation`, `REMInvocableProtocol`, and `A.ClientResult`
- `REMCustomSmartListFilterDescriptor` with throwing decode/encode, `v1`/`v2` serialization, typed filters, and secure coding
- Section-membership and section-ordering protocols

Targeted layouts recovered:

- `REMGroceryCategory.SuggestionStorageValues`: two strings at offsets `0x0` and `0x10`.
- `REMCustomSmartListFilterDescriptor`: operation and optional filter fields spanning offsets `0x8` through `0x88`.
- List fetch configuration: 50-byte value with 56-byte stride when embedded.
- iOS 27 list-section invocation adds an explicit grocery-section boolean at offset `0x8`.
- `REMObjectID_Codable` layout remained unavailable because it derives from a C-imported Objective-C class.

## Read-only experiments

All experiments ran headlessly from temporary binaries and never called save.

- Fetched 10 lists through `enumerateAllListsWithBlock:`.
- Fetched 1,617 reminders through `enumerateAllRemindersWithBlock:`.
- Primary-account list traversal returned 9 lists, 1,600 reminders, and 28 sections.
- Current SQLite export returned 1,639 reminders; `stats` saw 1,644 total reminders, 10 lists, and 34 sections.
- A real list object ID round-tripped successfully.
- Unsaved list, reminder, section, grocery, and custom-smart-list change objects were created and inspected.
- Temporary helper protocol handshake returned version 1.
- ReminderKit enumeration averaged 0.38 seconds in the initial five-run probe; full SQLite JSON export averaged 5.86 seconds.

That performance comparison is directional, not equivalent. The SQLite export includes more fields, and ReminderKit returned fewer reminders. It must not replace SQLite yet.

## Follow-up options

| Rank | Opportunity | Benefit | Confidence | Risk |
|---:|---|---|---|---|
| 1 | Add a manual metadata/runtime drift audit with normalized watched contracts | High | High | Low |
| 2 | Support the current grocery selector after an approved disposable write test | High for fallback reliability | High that drift exists | Medium |
| 3 | Add a read-only per-feature capabilities action to `remctl-private` | Medium-high | High | Low |
| 4 | Repair or explicitly disable built-in smart-list pinning on unsupported hosts | Medium | High | Medium-high |
| 5 | Prototype ReminderKit reads as a shadow path and compare results with SQLite | Potentially high speed/maintenance gain | Medium | Medium-high |

`REMCustomSmartListFilterDescriptor` is valuable for offline contract checks and fixture validation. Direct production use ranks below these five because it would add a new `ReminderKitInternal` dependency and its generated interface is not compiler-valid.

## False leads and tool limitations

- Generated Swift is not source code. Host and Simulator interfaces each produce eight parser diagnostics: unescaped `default`, malformed `init<>`, and one opaque `some` without a result type.
- The current host generated interface skips 28 C-imported types. Those include much of RemCTL’s actual Objective-C surface.
- Zero Swift types in ReminderKit does not mean zero API.
- The newest Simulator’s ReminderKit executable is cache-only, but the installed runtime has no inspectable cache file. Its UUID and Objective-C surface are therefore unverifiable.
- Simulator contracts are not macOS contracts. The host framework version, `4046.21`, is newer than Simulator `4043`.
- Swift metadata proves ABI declarations. It does not prove entitlements, TCC behavior, headless compatibility, CloudKit synchronization, or production safety.
- Field layouts are useful only for selected Swift values. Reimplementing Objective-C objects from offsets is unsupported.
- Using the recovered async APIs would add a private `ReminderKitInternal` dependency.
- The read APIs did not return the same reminder/section coverage as SQLite.
- At the time of the original read-only audit, compilation proved only that a declaration was linkable. The follow-up above separately tests the grocery write semantics and keeps built-in smart-list pinning disabled where the generic fetch is absent.

## Proposed `private-api-audit` workflow

Run it:

- Before a macOS update, to preserve a known-good baseline.
- Immediately after the update.
- Before releases that touch `remctl-private.m`.
- When a private helper returns an unknown-selector, object-ID, fetch, or save error.
- Against the newest and previous installed Simulator runtimes when their versions differ materially.

Inspect:

- Host `ReminderKit` Objective-C runtime.
- Host `ReminderKitInternal` Swift metadata.
- Newest and previous Simulator `ReminderKitInternal` binaries.
- `remctl-private` linked frameworks and undefined Objective-C classes.
- Real unsaved change objects, without calling save.

Small watched-symbol list:

- `REMObjectID`: construction, UUID, URL representation.
- `REMStore`: account, reminder/list/section/template/custom-smart fetches; all-list/all-reminder enumeration.
- `REMSaveRequest`: update/add methods and synchronous save.
- List and smart-list appearance/pin setters.
- Ordinary-list reminder ordering getters and before/after insertion selectors.
- `REMManualOrdering` initialization and custom-smart-list manual-order updates.
- Both grocery categorization selector spellings.
- Smart-list filter/version setters.
- Section ordering and membership setters.
- Tag, attachment, subtask, assignment, flag, urgent, and Early Reminder contexts.
- Swift typed fetches, `REMSaveRequest.save(executor:)`, smart-list descriptor serialization, and grocery canonical-name APIs.

Retain only compact artifacts:

- `environment.json`: OS, runtime, architecture, UUIDs, framework versions, tool version.
- `objc-contract.json`: watched selectors and type encodings.
- `swift-contract.json`: normalized selected declarations, async/throws, generic constraints, and conformances.
- `quality.json`: extraction counts, skipped C imports, parser errors, timing.
- `diff.json` or a compact Markdown summary.
- Do not retain full multi-megabyte interfaces or snapshots in the repository.

Fail the audit when:

- A production-used class disappears.
- A selector neither exists nor responds on a real unsaved object.
- A scalar ABI encoding changes.
- Object-ID construction or round-trip fails.
- A read-only store fetch fails unexpectedly.
- A watched contract has an unexplained breaking change.
- Baseline compilation or unit tests fail.

Warn, but do not fail, for:

- Unrelated private ABI additions/removals.
- Generated-interface syntax errors already classified as reconstruction defects.
- C-imported declarations skipped by MachOSwiftSection.
- Simulator-only changes.
- Layout changes in types RemCTL does not instantiate.
- New APIs with no headless or entitlement proof.

## Original recommendations and follow-up disposition

The original read-only audit deferred implementation until disposable writes were approved. The cross-version follow-up resolved each item:

1. `REMColor.colorSpace` changed from `NSInteger` to `NSUInteger`.
2. The grocery branch adds the proven Golden Gate receiver/argument contract and preserves Tahoe's shipped path. The rejected Tahoe alternative and the Golden Gate branch both received disposable write/readback/cleanup tests; only Golden Gate's new path passed without destructive side effects.
3. The generic smart-list fetch declaration remains as a capability check for other hosts. Tested hosts that lack it reject built-in pinning before creating a save request; custom targets never receive the built-in ID.
4. The helper has a read-only capability action with watched selector availability, normalized encodings, and `saveCalled: false`.
5. `applyPrivateMetadataToChange` and its four unreferenced helpers were removed. The private location-alarm fallback remains because it still protects hosts without the EventKit bridge.
6. The manual audit workflow remains intentionally manual. The complete reconstructed Swift ABI is not a CI gate.

## Reproduction commands

```bash
cd /path/to/remctl
AUDIT_DIR="$(mktemp -d /tmp/remctl-private-api-audit.XXXXXX)"

git status --short --branch
swift-section --version
sw_vers
uname -m
xcrun simctl list runtimes

env PYTHONPYCACHEPREFIX="$AUDIT_DIR/pycache" \
  python3 -m py_compile remctl remctl_*.py scripts/*.py tests/*.py

env PYTHONPYCACHEPREFIX="$AUDIT_DIR/test-pycache" \
  python3 -m unittest discover -s tests

xcrun swiftc -O -framework EventKit -framework Foundation \
  -o "$AUDIT_DIR/remctl-bridge" remctl-bridge.swift

xcrun swiftc -O -framework EventKit -framework Foundation \
  -o "$AUDIT_DIR/remctl-permissions" remctl-permissions.swift

xcrun clang -fobjc-arc -O -F/System/Library/PrivateFrameworks \
  -framework Foundation -framework AppKit -framework ReminderKit \
  -o "$AUDIT_DIR/remctl-private" remctl-private.m

./remctl --version
./remctl doctor --for-agent --json
otool -L "$AUDIT_DIR/remctl-private"

/usr/bin/dyld_info -uuid -platform \
  /System/Library/PrivateFrameworks/ReminderKit.framework/ReminderKit
/usr/bin/dyld_info -uuid -platform \
  /System/Library/PrivateFrameworks/ReminderKitInternal.framework/ReminderKitInternal

swift-section interface \
  --uses-system-dyld-shared-cache --architecture arm64e \
  --cache-image-name ReminderKit \
  -o "$AUDIT_DIR/ReminderKit.swiftinterface"

swift-section interface \
  --uses-system-dyld-shared-cache --architecture arm64e \
  --cache-image-name ReminderKitInternal \
  -o "$AUDIT_DIR/ReminderKitInternal.swiftinterface"

xcrun swiftc -frontend -parse "$AUDIT_DIR/ReminderKitInternal.swiftinterface"

python3 /path/to/inspect-swift-binaries/scripts/inspect_framework.py \
  ReminderKitInternal --runtime-build 24A5390f --mode interface \
  --architecture arm64 --layout --force \
  --output "$AUDIT_DIR/ReminderKitInternal-iOS27-layout.swiftinterface"

swift-section snapshot -a arm64 \
  --label "iOS Simulator 26.4 23E254a" \
  -o "$AUDIT_DIR/iOS26.4.snapshot.json" \
  "/Library/Developer/CoreSimulator/Volumes/iOS_23E254a/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS 26.4.simruntime/Contents/Resources/RuntimeRoot/System/Library/PrivateFrameworks/ReminderKitInternal.framework/ReminderKitInternal"

swift-section snapshot -a arm64 \
  --label "iOS Simulator 27.0 24A5390f" \
  -o "$AUDIT_DIR/iOS27.snapshot.json" \
  "/Library/Developer/CoreSimulator/Volumes/iOS_24A5390f/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS 27.0.simruntime/Contents/Resources/RuntimeRoot/System/Library/PrivateFrameworks/ReminderKitInternal.framework/ReminderKitInternal"

swift-section diff \
  "$AUDIT_DIR/iOS26.4.snapshot.json" "$AUDIT_DIR/iOS27.snapshot.json" \
  --summary-only

git diff --check
git status --short --branch
```

The audit’s temporary probe sources and raw evidence remain under `/tmp/remctl-private-api-audit.UokCTM` for the lifetime of that temporary directory.
