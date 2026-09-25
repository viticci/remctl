# Installed RemCTL 2.0 runtime review

Reviewed the Mac's installed RemCTL 2.0 client against local `main` at
`ae15dd8`. The ten Python/client/widget files matched the checkout byte for
byte. The branch was 14 commits ahead of the recorded `origin/main`; this
review did not fetch, push, or publish anything.

RemCTL lets Federico and his AI apps read and change Apple Reminders. Its
installed client lives in `~/bin`. A signed app in `~/Applications` holds the
macOS permissions. The local MCP server accepts tool calls from AI apps. An
optional HTTP service starts automatically and serves other devices through
Tailscale. Each tool should return an accurate result, affect only its own
request, and keep permission-bearing work inside the signed host.

The review covered the unpushed changes, request parsing and authentication,
process cancellation, tool arguments and results, config writes, the widget's
handling of untrusted content, date/alarm changes, and install/service behavior.
Security probes used isolated servers and synthetic data. The installed
endpoint received a bounded authentication check and one disposable reminder
test, with verified cleanup.

## Findings and fixes

1. **HTTP resource exhaustion and ambiguous body lengths — high priority.**
   The HTTP service read the body before checking the bearer token. A client
   could declare a large body, send none, and hold a thread without a token.
   A negative `Content-Length` reached `read(-1)`, which reads until EOF instead
   of enforcing the 4 MiB limit. Duplicate lengths and transfer encoding were
   not rejected. Threads and legacy sessions had no count limits. The service
   now authenticates first, closes rejected connections, validates one bounded
   length, limits connections to 32 and sessions to 256, and expires idle
   sessions after an hour. Socket tests verify rejection before sending a body.
   This was an availability issue; the review did not find an authentication
   bypass that disclosed reminders.

2. **Request IDs crossed client boundaries — high priority.**
   The HTTP server shared one subprocess map keyed only by the client's request
   ID. For example, two clients using request `1` could overwrite the entry;
   cancelling `1` could stop the other client's call, and completion could be
   mislabeled as cancellation. IDs now include the connection/session scope.
   Duplicate active IDs are refused. Stateless HTTP requests have independent
   scopes and cannot cancel another request by guessing an ID. Tests cover
   separate sessions and a real subprocess with a duplicate active ID.

3. **Cancellation could crash the server or miss a queued write — medium priority.**
   `notifications/cancelled` accepted an array as `requestId`, then used it as
   a dictionary key outside the request error handler. This disconnected stdio.
   Cancellation also did nothing while a call waited for a worker. The server
   now validates cancellation IDs and registers queued calls before dispatch.
   A cancelled queued call returns without starting a subprocess. Malformed
   headers and invalid Unicode receive errors without killing the connection;
   deep JSON and response encoding have defensive handling too.

4. **MCP could not clear notes — medium priority.**
   `update_reminder` treated `notes: ""` as an omitted option. The CLI supports
   an empty notes value, but it never received one. The argument builder now
   omits only `None`. A live disposable reminder was created with notes, updated
   with an empty string, read back with empty notes, and deleted. Search then
   verified that it was gone.

5. **Partial writes lost their recovery data — medium priority.**
   A partly successful import exits with status 1 and prints a structured
   summary, including `createdIds` and per-item errors. MCP discarded that
   summary in favor of stderr progress text. A client could then retry items
   that already existed. MCP now keeps the partial result and marks it as an
   error. Tests verify that created IDs and retry details survive. This also
   preserves the result of a private add that fails after creating its reminder.

6. **Temporary files could overwrite unrelated files or expose config contents — medium priority.**
   Client registration used a predictable `.remctl-tmp` path; bundle creation
   used `.tmp`. Both followed a preexisting symbolic link. Config content was
   written before its private mode was applied. Both writers now use exclusive,
   private temporary files. Backups also start private and have unique names.
   Tests place links at the former temporary paths and verify that the target
   files remain unchanged. These paths require local file access; they are not
   an unauthenticated HTTP exploit.

7. **Private config writes could leave a truncated token file — medium priority.**
   The shared writer truncated the existing file before writing the replacement.
   An interrupted write could destroy the previous config, and a concurrent
   startup could treat it as missing. The writer now writes and syncs a private
   temporary file before replacing the destination. A failed-publish test
   verifies that the previous token content remains intact and the temporary
   file is removed.

8. **Service commands could claim success after failure — medium priority.**
   Token rotation reported success even when the active HTTP service failed to
   restart, leaving the old token potentially accepted. The fallback restart in
   HTTP installation ignored its exit status, and Tailscale installation reported
   success after all health checks failed. These paths now report failure. Token
   rotation explicitly reports a saved token and a failed service restart. Tests
   simulate failure without rotating the real credential or changing Tailscale.
   HTTP also reads the token file on each request, so manually started endpoints
   revoke old tokens without a service restart. An isolated test verifies old
   token rejection, new token acceptance, and denial when the config disappears.

9. **Installed HTTP code stayed stale until a manual restart — upgrade gap.**
   The installer replaced files while the HTTP process kept its imported code.
   Installation now reloads an already-loaded endpoint after publishing, then
   checks a different process ID and a healthy response. The local upgrade
   exercised this path successfully. Existing stdio connections still need the
   AI client to reconnect; installation does not terminate other conversations.

## Verification

- The original suite passed 689 tests before changes.
- The expanded full suite passed 709 tests on the CLI's Python 3.14. All 124
  focused MCP/runtime checks also passed on the signed host's protected Python
  3.13. Python 3.10 syntax compatibility and installer shell syntax were checked.
- The normal installer rebuilt and strictly verified the signed app, preserved
  its signing identity, published the client files, and reloaded the HTTP service.
- The live endpoint rejected an unauthenticated incomplete body with 401 and a
  negative body length with 400. An authenticated MCP doctor call reached the
  signed host. The notes-clearing write and cleanup passed.
- Ten installed client/module/widget files matched the source, and all 23
  installer manifest entries were verified.
- The endpoint bound to `127.0.0.1:7362`. Tailscale served `/remctl` on port 443;
  Funnel was not enabled for that port. Other Tailscale routes were unchanged.

The signed host had authorized Reminders and Full Disk Access permissions.
Automation reported `targetNotRunning` while Reminders.app was closed, so this
review does not claim a live AppleScript flagging test. It did not run the full
private-metadata write matrix or change existing reminders. This is a code and
runtime review, not a claim that every possible security issue is excluded.
