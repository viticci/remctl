---
name: onboarding
description: Set up the standalone RemCTL desktop plugin and its local Apple Reminders access.
---
Use this plugin's RemCTL tools. Never route this plugin through Mac Remote or any other remote adapter.

1. Call `doctor` and read `access.effective`. The signed RemCTL Capability Host owns the macOS permissions. Never tell users to grant access to Python, Terminal, or Codex.
2. If the host is missing, explain that RemCTL itself must be installed first: the release download, or `./install.sh --from-source` from the repository. If access is blocked, name the missing permission and tell the user to run `remctl onboard` in Terminal. Do not say setup succeeded until `doctor` reports ready. Contacts is optional: only `messaging_contact` with a person's name needs it. If a call fails with `contacts_access_required`, tell the user to run `remctl permissions contacts` in Terminal, or pass the person's phone number or email instead.
3. Call `open_workspace`. Point the user to RemCTL's settings in the workspace, where they can pick a default list for new reminders and turn on 'Advanced Reminders features' for sections, tags, templates, and other private ReminderKit features. Mention that those features use private Apple APIs that can change with macOS updates. You have no tool to change these settings; the user changes them.
4. Explain in one sentence that selecting reminders and choosing Attach shares only those reminders with the conversation. The workspace is desktop-only.

If Codex also shows a separate RemCTL connection from an older `remctl mcp install`, or 'Reminders' is missing from the sidebar, tell the user to remove that connection with `remctl mcp remove --client codex` and restart Codex. That leaves other apps' connections alone.
