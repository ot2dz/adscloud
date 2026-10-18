# Meta Ads Dashboard (HTTP engine — no browser)

One web project that runs anywhere (VPS / Coolify), reachable via a domain, and
scrapes the Meta Ad Library over **HTTP/GraphQL** — **no Playwright, no Chromium**.

It combines:

- the Flask dashboard + database + job queue + browser-extension API of the
  original `MetaAdsPlaywright` project, and
- the lightweight HTTP scraping engine (`meta_ads_collector`) developed in
  `E:\opencode\meta`.

## What changed vs. the Playwright version

- `collector/scraper.py` (Playwright) → **`engine.py`** (HTTP engine).
- No `worker.py`, no `playwright` / `playwright-stealth` dependencies.
- The job queue is kept (for progress / schedule / history) but is executed by an
  **in-process thread** (`job_worker_loop`) instead of a separate worker.

## Layout

```
app.py                  # Flask app: dashboard UI + APIs + in-process job runner
engine.py               # HTTP scrape() adapter (uses meta_ads_collector)
db.py                   # DB layer (PostgreSQL via DATABASE_URL, SQLite fallback)
pages.py / slicing.py   # target URL builders + date slicing
notify.py               # Telegram/webhook notifications
store_filter.py         # store-domain extraction
meta_ads_collector/     # vendored HTTP/GraphQL engine (modified: date filters)
templates/index.html    # dashboard (ads / targets / tracked pages / stores / jobs / settings)
Dockerfile              # production image (gunicorn, 1 worker)
docker-compose.yaml     # Coolify service
requirements.txt
```

## Run locally

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python app.py
# open http://127.0.0.1:5002
```

Optional env (PowerShell):

```powershell
$env:DATABASE_URL="postgresql://..."   # else SQLite
$env:META_ADS_PROXY="gw.host:port:user:pass"
$env:INGEST_TOKEN="change-me"
```

## Deploy on Coolify

1. Push this folder to a Git repository.
2. Coolify → new **Application** from that repo (build pack: **Dockerfile** or
   the included `docker-compose.yaml`). Port **5002**.
3. Environment variables:
   - `DATABASE_URL` = the Coolify PostgreSQL connection string (`metaads-db`).
   - `META_ADS_PROXY` = your residential/rotating proxy.
   - `INGEST_TOKEN` = a secret (optional but recommended).
4. Set the domain (e.g. `https://ads.botdz.com`) → Coolify issues HTTPS.

## Browser extension

In the extension popup → **ADRA Cloud**:

- **Cloud API URL** = your domain (e.g. `https://ads.botdz.com`).
- Sign in with your dashboard email and password. The extension then gets a
  personal token, so every saved ad and tracked page belongs to that account.
- Test connection, then use **تتبع الصفحة** / **تحديث الرصد** / **حفظ الإعلان**.

## Accounts

- Each user sees only the ads they saved and the pages they track (plus every
  ad the server collects for those pages).
- A page tracked by several users is refreshed by the server once and shown to
  all of them; it is only removed when the last user stops tracking it.
- Admins see everything and can pick one user (by email) from the dashboard to
  see just that user's data. Keyword scraping, targets, jobs and settings are
  admin-only.
- Keep `INGEST_TOKEN` set in production: it blocks anonymous writes. It still
  works as a shared legacy token, but data sent with it has no owner.

Tracked pages and their snapshots appear under the dashboard's
**«الصفحات المتتبعة»** tab.
