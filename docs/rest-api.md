# REST API

`remctl-server` exposes RemCTL over HTTP. It is optional and binds to localhost by default.

## Start

```bash
remctl-server
remctl-server --host 127.0.0.1 --port 19876
remctl service install
remctl service status
```

Generate or rotate a token:

```bash
remctl-server --generate-token
```

The token lives at:

```text
~/.config/remctl/api-token
```

## Authentication

`/health` is public. `/api/v1/*` requires Bearer auth.

```bash
TOKEN="$(cat ~/.config/remctl/api-token)"
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:19876/api/v1/today
```

## Read Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/v1/lists` | Lists with counts |
| `GET` | `/api/v1/lists/:name` | Reminders in a list |
| `GET` | `/api/v1/lists/:name/sections` | Sections for a list |
| `GET` | `/api/v1/reminders/:id` | Reminder detail |
| `GET` | `/api/v1/reminders/:id/subtasks` | Subtasks |
| `GET` | `/api/v1/today` | Due today and overdue |
| `GET` | `/api/v1/upcoming?days=7` | Upcoming reminders |
| `GET` | `/api/v1/overdue` | Overdue reminders |
| `GET` | `/api/v1/flagged` | Flagged reminders |
| `GET` | `/api/v1/urgent` | macOS 26 urgent reminders |
| `GET` | `/api/v1/search?q=query` | Search title and notes |
| `GET` | `/api/v1/tags` | Tags |
| `GET` | `/api/v1/sections` | Sections |
| `GET` | `/api/v1/stats` | Counts |
| `GET` | `/health` | Health check |

## Write Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/v1/reminders` | Create reminder |
| `PATCH` | `/api/v1/reminders/:id` | Update reminder |
| `DELETE` | `/api/v1/reminders/:id` | Delete reminder |
| `POST` | `/api/v1/reminders/:id/complete` | Complete |
| `POST` | `/api/v1/reminders/:id/uncomplete` | Mark incomplete |
| `POST` | `/api/v1/reminders/:id/flag` | Flag |
| `POST` | `/api/v1/reminders/:id/unflag` | Unflag |
| `POST` | `/api/v1/lists` | Create list |
| `PATCH` | `/api/v1/lists/:name` | Rename list |
| `DELETE` | `/api/v1/lists/:name` | Delete list |

## Create Example

```bash
TOKEN="$(cat ~/.config/remctl/api-token)"
curl -X POST http://127.0.0.1:19876/api/v1/reminders \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Buy milk",
    "list": "Shopping",
    "dueDate": "2026-05-05T14:00:00",
    "priority": "high",
    "recurrence": {"frequency": "weekly", "daysOfWeek": [2]}
  }'
```

## Service Notes

The service has its own macOS privacy grants. If `/health` says the database is not found but the CLI works, use the visual helper:

```bash
remctl permissions full-disk-access --scope service
```

Manual fallback: grant Full Disk Access to the Python interpreter printed by:

```bash
remctl service status
```

Then run:

```bash
remctl service restart
remctl doctor
```
