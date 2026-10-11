# PrismXE — merged field-operations app

One Flask app (one Render service) combining three previously separate apps:

| Prefix | App | What it does |
|---|---|---|
| `/gauge/` | Boiler Gauge Check | Photograph boiler/pump gauges, auto-read needles (OpenCV), log measurements, Google Sheets sync |
| `/cm/` | Construction Manager | 11 tabs: dashboard, sites, blueprints, workers, timecards, incidents, daily-logs, labour, materials, QA, data + `/cm/onsite` worker app (QR clock in/out, offline queue, live location) |
| `/patrol/` | Patrol Board | Equipment patrol timetable dashboard, worker clock in/out, timecards, `/api/patrol-schedule` JSON feed |

`GET /` is a portal page with three cards linking to `/gauge/`, `/cm/`, `/patrol/`.

## Run

```
pip install -r requirements.txt
gunicorn app:app
```

`app.py` binds `$PORT` (default 5000) on `0.0.0.0`.

## Routes

- `/` — portal
- `/gauge/`, `/gauge/inspect`, `/gauge/e/<equip>`, `/gauge/admin`, `/gauge/api/*`
- `/cm/`, `/cm/sites`, `/cm/blueprints`, `/cm/workers`, `/cm/timecards`, `/cm/incidents`, `/cm/daily-logs`, `/cm/labour`, `/cm/materials`, `/cm/qa`, `/cm/data`, `/cm/onsite`, `/cm/clock/<token>`, `/cm/sw.js`, `/cm/api/*`
- `/patrol/`, `/patrol/settings`, `/patrol/timecard`, `/patrol/patrol-clock`, `/patrol/qr.png`, `/patrol/sw.js`, `/patrol/api/patrol-schedule` (CORS `*`)

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `BASE_URL` | `https://app.prismxe.com` | Public base URL encoded in QR codes (e.g. `…/cm/clock/<token>`) |
| `SHEETS_URL` | — | Google Apps Script web-app URL; every gauge log and settings save POSTs here, and gauge config auto-restores from it on startup |
| `SECRET_KEY` | dev key | Flask session signing — set a real value in production |
| `ADMIN_PASSWORD_HASH` | built-in default | SHA-256 of the patrol-board admin password; override to rotate |
| `PORT` | `5000` | HTTP port |

## Data files (local JSON, ephemeral on Render free tier — back up before deploys)

- `data_gauge.json` — equipment + gauge calibration (also restores from Google Sheets via `SHEETS_URL`)
- `data_cm.json` — sites, workers, timecards, incidents, logs, labour, materials, QA, snapshots
- `data_patrol.json` — timetable slots, equipment, shifts, workers, punches

Gauge readings log: `data/gauge_readings.csv`. Uploads: `uploads/gauge/`, `static/cm/uploads/`.

## Merge method

Flask **blueprints with `url_prefix`** (`/gauge`, `/cm`, `/patrol`), one per original app.
Templates namespaced as `templates/gauge/*`, `templates/cm/*`, `templates/patrol/*`
(all `render_template` calls and `{% extends %}` use the qualified path, because Flask
searches blueprint template folders in registration order, not by current blueprint).
Static namespaced as `static/cm/*`, `static/patrol/*` (gauge has no static assets).
Session keys namespaced (`gauge_admin`, `patrol_admin` — both were `"admin"` before).
Service-worker precache paths and QR-code URLs rewritten to the new prefixes.
