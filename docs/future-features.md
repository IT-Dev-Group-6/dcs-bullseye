# Future Features

## Completed

| Feature | Notes |
|---------|-------|
| ~~Player join/leave notifications~~ ✅ | Analytics pipeline — agent pushes events to orchestrator every 60s |
| ~~Alerts when a server crashes repeatedly~~ ✅ | Crash loop detection in EventsCog — 3 crashes/10 min triggers alert |
| ~~Player count history / analytics~~ ✅ | `/dcs stats` — sessions, players, missions, maps, peak hours (EST/PST) |
| ~~Player self-service stats~~ ✅ | `/dcs register` + `/dcs mystats` — personal stats per pilot name |
| ~~Per-player session duration tracking~~ ✅ | Session pairing (join→leave) — avg and total time in `/dcs mystats`, avg in `/dcs stats` |

---

## Planned

| # | Feature | Difficulty | Notes |
|---|---------|------------|-------|
| 1 | Community host status in embed | Medium | Show external servers configured without an agent with richer info beyond TCP ping — already shown in status embed but could be improved |
| 2 | Auto-update schedule | Medium | Run DCS update automatically at a configured time instead of manual `/dcs update` trigger |
| 3 | Scheduled mission rotation | Medium | Auto-cycle through a mission playlist on a timer — configurable per instance |
| 4 | Mission vote / rotation command | Hard | Let players vote to change the active mission via Discord; majority wins |
| 5 | Player join/leave Discord notifications | Medium | Post a message when a player joins or leaves a server (requires threshold to avoid spam) |
| 6 | Analytics retention / pruning | Easy | Auto-delete analytics events older than N days to keep DB small |
| 7 | `/dcs stats` trend comparison | Medium | Compare this week vs last week for player counts and activity |
| 8 | One-command deployment | Hard | `bullseye deploy --preset pvp` — install DCS Server + SRS + Tacview, configure ports, install common mods, set up auto-restart, all in one command. Dramatically simplifies new server setup. |
| 9 | Mission sync from Git | Medium | `bullseye mission sync <repo-url>` — auto-pull missions from a GitHub repo, install them, rotate on update. Enables CI/CD for mission files. |
| 10 | Auto port assignment for new instances | Medium | When adding a second instance to a host, automatically pick non-conflicting DCS/WebGUI/Tacview ports. Tied to multi-instance installer (Issue #13). |
| 11 | Local Python CLI (`typer`+`rich`) | Medium | `dcs-bullseye start server1` etc. — CLI frontend for headless/scripted use without Discord. Lower priority since Discord bot covers most needs. |
| 12 | Admin web panel | Medium | Password-protected web UI for DB management. See plan below. |
| 13 | Full log download | Easy | `/dcs logs` currently tails N lines — add ability to download the complete `dcs.log` as a file attachment in Discord. |

---

## Admin Web Panel — Design Plan

**What it is:** Server-rendered HTML panel mounted at `/admin/` inside the existing orchestrator FastAPI process. No separate service, no build step, no external CDN.

**Auth:** Login form — password = orchestrator `api_key` (no new config). Sets an `httponly` session cookie (`samesite=lax`, 24h). Single session; new login invalidates old one. Stored as a token in `app.state.admin_session_token`.

**New dependencies (orchestrator/requirements.txt):**
- `jinja2>=3.1` (already transitively installed by starlette/FastAPI, just pin it)
- `python-multipart>=0.0.9` (needed for form POSTs)

**New DB methods to add in `database.py`:**
| Method | SQL |
|--------|-----|
| `delete_instance(id)` | `DELETE FROM instances WHERE id = ?` |
| `delete_invite(id)` | `DELETE FROM invite_codes WHERE id = ?` |
| `prune_audit_logs(days)` | `DELETE FROM audit_logs WHERE timestamp < cutoff` |
| `prune_analytics_events(days)` | `DELETE FROM analytics_events WHERE timestamp < cutoff` |

**New files:**
```
orchestrator/api/routes/admin.py          ← all admin routes
orchestrator/api/templates/admin/
  base.html        ← sidebar layout + CSS (no external deps)
  login.html       ← standalone login form
  dashboard.html   ← row counts + recent activity
  hosts.html       ← hosts table + delete
  instances.html   ← instances table + delete
  invites.html     ← invites table + create + delete
  audit.html       ← audit logs, filter, prune form
  analytics.html   ← analytics events, filter, prune form
```

**Mount in `app.py`:**
```python
from .routes import admin as admin_routes
app.include_router(admin_routes.router, prefix="/admin")
# No _AUTH_DEP — admin uses its own cookie-based session
```

**Pages and operations:**

| Page | URL | Operations |
|------|-----|------------|
| Dashboard | `GET /admin/` | Row counts for all tables, 5 most recent audit/analytics rows, quick nav links |
| Hosts | `GET /admin/hosts` | Table: id, name, agent_url, enabled, last_seen, frp_port, instance count |
| | `POST /admin/hosts/{id}/delete` | Delete host + cascades to instances |
| Instances | `GET /admin/instances` | Table: id, host name, service_name, name, created_at |
| | `POST /admin/instances/{id}/delete` | Delete orphan instance |
| Invites | `GET /admin/invites` | Table: code, host_name, used, used_by, created_at, expires_at |
| | `POST /admin/invites/create` | Create new invite (optional host_name field) |
| | `POST /admin/invites/{id}/delete` | Delete invite |
| Audit Logs | `GET /admin/audit` | Filter by instance_id / host_id / limit; shows timestamp, actor, action, status, detail |
| | `POST /admin/audit/prune` | Delete all rows older than N days (default 30) |
| Analytics | `GET /admin/analytics` | Filter by host_id / event_type / limit |
| | `POST /admin/analytics/prune` | Delete all rows older than N days (default 90) |

**UI style:** Clean dark sidebar (#16213e), white content area. Pure HTML/CSS — no JS frameworks, no CDN. Responsive tables, flash messages via query param, delete buttons use `<form method="POST">` (no JS confirm — just bold red button).

**Security notes:**
- All POST routes check cookie before acting
- `secrets.compare_digest` for both password check and cookie validation
- Cookie is `httponly=True, samesite="lax"` — no CSRF token needed for samesite=lax + POST
