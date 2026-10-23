# Development notes

These are dated engineering records: audits, reviews, and the evidence behind each validation pass. Each one describes RemCTL as it was on that date, and none of them are kept up to date. For how RemCTL works today, read the [main docs](../../README.md#documentation).

| Note | What it covers |
| --- | --- |
| [Private API audit](private-api-audit-2026-08-12.md) | ReminderKit calls, cross-version contracts, and rejected alternatives |
| [macOS 27 review](macos27-compat-review.md) | Compatibility with macOS 27 |
| [Runtime review](runtime-review-2026-09-25.md) | MCP and runtime hardening |
| [remindctl comparison](remindctl-comparison-2026-09-26.md) | RemCTL versus the CLI behind Hermes Agent's Reminders skill |
| [2.0 launch validation](release-2.0.0-validation.md) | Redesigned launch app, notarization, and installed acceptance |
| [2.0.2 validation](release-2.0.2-validation.md) | The installer app, 1.7.1 upgrades through the download, and notarization |
| [2.0.3 validation](release-2.0.3-validation.md) | The tailnet token in each device's Keychain, tested live with four clients |
| [2.0.4 validation](release-2.0.4-validation.md) | The tailnet install summary crash and its fix |
| [2.1.0 validation](release-2.1.0-validation.md) | The Claude Code plugin, the completed-search fix (#54), and plugin-aware installs |
| [2.2.0 validation](release-2.2.0-validation.md) | No Priority, performance and date fixes, notarized download, and installed acceptance |
| [2.2.1 validation](release-2.2.1-validation.md) | The three contributor fixes, verified 1.7.1 upgrades, notarized download, and installed acceptance |
| [2.4.0 validation](release-2.4.0-validation.md) | Subtask nesting, Smart List sections, eleven merged contributor PRs, issue fixes, notarized download, and live acceptance across every MCP surface |
| [October 2 audit](audit-2026-10-02.md) | Smart-list and statistics performance, date consistency, safe recurring completion, installer packaging, and cross-client checks |
| [PR fixes and 1.7.1 upgrades](pr-fixes-2026-10-02.md) | Archived colors, plugin tabs, renamed iCloud accounts, and the reproduced legacy upgrade bug |
| [Distribution validation](distribution-validation-2026-09-30.md) | The notarized download, free builds, signing, and permission continuity |
| [Desktop validation](desktop-validation-2026-09-30.md) | Acceptance testing of the Codex plugin |
| [Events](events-2026-09-30.md) | The MCP Events implementation and why it's turned off |
| [Icon](icon-provenance.md) | How the app icon was made, and its source files |
